'''
Duper looks for duplicate packages within a single organization, updates the
retained package and removes the rest.
'''

from datetime import datetime
import itertools
import logging

from .ckan_api import CkanApiFailureException, CkanApiCountException
from . import util

module_log = logging.getLogger(__name__)

PACKAGE_NAME_MAX_LENGTH = 100


class ContextLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        kv_pairs = ' '.join('%s=%s' % (key, value) for key, value in self.extra.items())
        return '%s %s' % (msg, kv_pairs), kwargs


class Deduper(object):
    def __init__(self, organization_name, ckan_api, removed_package_log=None, duplicate_package_log=None, run_id=None):
        self.organization_name = organization_name
        self.ckan_api = ckan_api
        self.log = ContextLoggerAdapter(module_log, {'organization': organization_name})
        self.removed_package_log = removed_package_log
        self.duplicate_package_log = duplicate_package_log
        self.stopped = False

        if not run_id:
            run_id = datetime.now().strftime('%Y%m%d%H%M%S')

        self.run_id = run_id

    def dedupe(self):
        self.log.debug('Fetching dataset identifiers with duplicates')
        try:
            dataset_identifiers = self.ckan_api.get_duplicate_identifiers(self.organization_name)
        except CkanApiFailureException, exc:
            self.log.error('Failed to fetch dataset identifiers for organization')
            self.log.exception(exc)
            # continue onto the next organization
            return

        self.log.info('Found dataset identifiers with duplicates count=%d',
                      len(dataset_identifiers))

        self.log.debug('Fetching collection identifiers with duplicates')
        try:
            collection_identifiers = \
                self.ckan_api.get_duplicate_collection_identifiers(self.organization_name)
        except CkanApiFailureException, exc:
            self.log.error('Failed to fetch collection identifiers for organization')
            self.log.exception(exc)
            # continue onto the next organization
            return

        self.log.info('Found collection identifiers with duplicates count=%d',
                      len(collection_identifiers))

        # For both collection and dataset identifiers, treat them the same and
        # dedupe them together. Map them to the identifier name, since that's
        # all we're interested in. We use a frozenset because we want unique
        # identifiers and will treat it as immutable.
        identifiers = frozenset(i['name'] for i in dataset_identifiers + collection_identifiers)

        duplicate_count = 0
        count = itertools.count(start=1)
        for identifier in identifiers:
            if self.stopped:
                return

            self.log.info('Deduplicating identifier=%s progress=%r',
                          identifier, (next(count), len(identifiers)))
            try:
                duplicate_count += self.dedupe_identifier(identifier)
            except CkanApiFailureException:
                self.log.error('Failed to dedupe harvest identifier=%s', identifier)
                continue
            except CkanApiCountException:
                self.log.error('Got an invalid count, this may not be a duplicate or there could '
                               'be index corruption identifier=%s', identifier)
                continue

        self.log.info('Summary duplicate_count=%d', duplicate_count)


    def remove_duplicate(self, duplicate_package, retained_package):
        self.log.info('Removing duplicate package=%r',
                      (duplicate_package['id'], duplicate_package['name']))
        if self.removed_package_log:
            self.removed_package_log.add(duplicate_package)

        if self.duplicate_package_log:
            self.duplicate_package_log.add(duplicate_package, retained_package)

        self.ckan_api.remove_package(duplicate_package['id'])

    def mark_retained_package(self, retained_package):
        '''
        Mark the retained package with a datagov_dedupe property in case we're
        interrupted. This allows us to continue with removing duplicates when we resume.

        Note: we're currently not mutating the data in a way that wouldn't be idempotent.
        '''
        self.log.info('Marking retained dataset for idempotency package=%r',
                      (retained_package['id'], retained_package['name']))
        util.set_package_extra(retained_package, 'datagov_dedupe',
                               self.run_id)

        # Call the update API
        self.log.debug('Mark retained package in API package=%r',
                       (retained_package['id'], retained_package['name']))
        self.ckan_api.update_package(retained_package)


    def commit_retained_package(self, retained_package):
        '''
        Unmarks the package for deduplication and commits any data changes.
        '''
        # Mark the retained package
        util.set_package_extra(retained_package, 'datagov_dedupe', None)
        util.set_package_extra(retained_package, 'datagov_dedupe_retained', self.run_id)

        self.log.debug('Commit retained package in API package=%r',
                       (retained_package['id'], retained_package['name']))
        self.ckan_api.update_package(retained_package)

    def dedupe_identifier(self, identifier):
        '''
        Removes duplicate datasets for the given identifier. The
        deduper is meant to be idempotent so that if it is interrupted, it can
        pick up where it left off without losing data.

        1. Get the number of datasets with this identifier.
           a. If there is only one dataset, no duplicates. Continue with next identifier.
        2. Fetch the oldest dataset which is to be retained.
        3. Mark the retained dataset as being processed.
        4. Fetch the datasets for this identifier in batches.
        5. For each dataset:
           a. Check if this is the retained dataset, in which we skip.
           b. Remove the dataset.
        6. Commit the retained dataset as being processed.

        We make sure the commit of the retained dataset happens last. This
        keeps the logging cleaner, since we don't want to confuse ourselves
        logging information that is potentially changing. This also means the
        same information is logged in dry-run vs read/write.

        Returns the number of duplicate datasets.
        '''

        log = ContextLoggerAdapter(
            module_log,
            {'organization': self.organization_name, 'identifier': identifier},
            )

        log.debug('Fetching number of datasets for unique identifier')
        dataset_count = self.ckan_api.get_dataset_count(self.organization_name, identifier)
        log.info('Found packages count=%d', dataset_count)

        # If there is only one or less, there's no duplicates.
        if dataset_count <= 1:
            log.debug('No duplicates found for harvest identifier.')
            return 0

        # We want to keep the oldest dataset
        self.log.debug('Fetching oldest dataset for harvest identifier=%s', identifier)
        retained_dataset = self.ckan_api.get_oldest_dataset(identifier)

        # Check if the dedupe process has been started on this package
        if not util.get_package_extra(retained_dataset, 'datagov_dedupe'):
            # We mark the retained package as having started the dedupe
            # process. This helps us record information like the original
            # package name so that in case we are interrupted, we can pick up
            # where we left off.
            #
            # If we're interrupted, it's possible the original dataset would've
            # been removed, so we need to make sure we collect all information
            # we need for the final retained update now.
            self.mark_retained_package(retained_dataset)

        # Fetch datasets in batches
        def get_datasets(total, rows=1000):
            '''
            Returns a generator for fetching additional packages in batches of :rows.
            '''
            start = 0
            while start < total:
                log.debug(
                    'Batch fetching datasets for harvest offset=%d rows=%d total=%d',
                    start, rows, total)
                datasets = self.ckan_api.get_datasets(self.organization_name, identifier, start, rows)
                if len(datasets) < 1:
                    log.warning('Got zero datasets from API offset=%d total=%d', start, total)
                    raise StopIteration

                start += len(datasets)
                for dataset in datasets:
                    yield dataset

        # Now we can collect the datasets for removal
        duplicate_count = 0
        for dataset in get_datasets(dataset_count):
            if self.stopped:
                self.log.debug('Deduper is stopped, cleaning up...')
                return duplicate_count

            if dataset['organization']['name'] != self.organization_name:
                log.warning('Dataset harvested by organization but not part of organization pkg_org_name=%s package=%r',
                            dataset['organization']['name'], (dataset['id'], dataset['name']))
                continue

            if dataset['id'] == retained_dataset['id']:
                log.debug('This package is the retained dataset, not removing package=%s', dataset['id'])
                continue

            duplicate_count += 1
            try:
                self.remove_duplicate(dataset, retained_dataset)
            except CkanApiFailureException, e:
                log.error('Failed to remove dataset status_code=%s package=%r',
                          e.response.status_code, (dataset['id'], dataset['name']))
                continue

        # Rename the retained package
        self.log.info('Committing retained package rename package=%r',
                      (retained_dataset['id'], retained_dataset['name']))
        self.commit_retained_package(retained_dataset)

        return duplicate_count


    def stop(self):
        '''
        Tells the Deduper to stop processing anymore records.
        '''
        self.stopped = True
