# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-2019 Bitergia
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
# Authors:
#     Santiago Dueñas <sduenas@bitergia.com>
#     Alvaro del Castillo San Felix <acs@bitergia.com>
#

import logging

import pickle
import rq

import perceval
import perceval.backend
import perceval.backends
import perceval.archive

from ._version import __version__
from .errors import NotFoundError


logger = logging.getLogger(__name__)


class JobResult:
    """Class to store the result of a Perceval job.

    It stores the summary of a Perceval job and other useful data
    such as the task and job identifiers, the backend and the
    category of the items generated.

    :param job_id: job identifier
    :param task_id: identifier of the task linked to this job
    :param backend: backend used to fetch the items
    :param category: category of the fetched items
    """
    def __init__(self, job_id, task_id, backend, category):
        self.job_id = job_id
        self.task_id = task_id
        self.backend = backend
        self.category = category
        self.summary = None

    def to_dict(self):
        """Convert object to a dict"""

        result = {
            'job_id': self.job_id,
            'task_id': self.task_id
        }

        if self.summary:
            result['fetched'] = self.summary.fetched
            result['skipped'] = self.summary.skipped
            result['min_updated_on'] = self.summary.min_updated_on.timestamp()
            result['max_updated_on'] = self.summary.max_updated_on.timestamp()
            result['last_updated_on'] = self.summary.last_updated_on.timestamp()
            result['last_uuid'] = self.summary.last_uuid
            result['min_offset'] = self.summary.min_offset
            result['max_offset'] = self.summary.max_offset
            result['last_offset'] = self.summary.last_offset
            result['extras'] = self.summary.extras

        return result


class PercevalJob:
    """Class for wrapping Perceval jobs.

    Wrapper for running and executing Perceval backends. The items
    generated by the execution of a backend will be stored on the
    Redis queue named `qitems`. The result of the job can be obtained
    accesing to the property `result` of this object.

    :param job_id: job identifier
    :param task_id: identifier of the task linked to this job
    :param backend: name of the backend to execute
    :param conn: connection with a Redis database
    :param qitems: name of the queue where items will be stored

    :rasises NotFoundError: raised when the backend is not available
        in Perceval
    """
    def __init__(self, job_id, task_id, backend, category, conn, qitems):
        try:
            self._bklass = perceval.backend.find_backends(perceval.backends)[0][backend]
        except KeyError:
            raise NotFoundError(element=backend)

        self.job_id = job_id
        self.task_id = task_id
        self.backend = backend
        self.conn = conn
        self.qitems = qitems
        self.archive_manager = None
        self.category = category

        self._big = None  # items generator
        self._result = JobResult(self.job_id, self.task_id,
                                 self.backend, self.category)

    @property
    def result(self):
        if not self._result.summary and self._big and self._big.summary:
            self._result.summary = self._big.summary
        return self._result

    def initialize_archive_manager(self, archive_path):
        """Initialize the archive manager.

        :param archive_path: path where the archive manager is located
        """
        if archive_path == "":
            raise ValueError("Archive manager path cannot be empty")

        if archive_path:
            self.archive_manager = perceval.archive.ArchiveManager(archive_path)

    def run(self, backend_args, archive_args=None):
        """Run the backend with the given parameters.

        The method will run the backend assigned to this job,
        storing the fetched items in a Redis queue. The ongoing
        status of the job, can be accessed through the property
        `result`.

        When the parameter `fetch_from_archive` is set to `True`,
        items will be fetched from the archive assigned to this job.

        Any exception during the execution of the process will
        be raised.

        :param backend_args: parameters used to un the backend
        :param archive_args: archive arguments
        """
        args = backend_args.copy()

        if archive_args:
            self.initialize_archive_manager(archive_args['archive_path'])

        self._result = JobResult(self.job_id, self.task_id,
                                 self.backend, self.category)

        self._big = self._create_items_generator(args, archive_args)

        for item in self._big.items:
            self._metadata(item)
            self.conn.rpush(self.qitems, pickle.dumps(item))

    def has_archiving(self):
        """Returns if the job supports items archiving"""

        return self._bklass.has_archiving()

    def has_resuming(self):
        """Returns if the job can be resumed when it fails"""

        return self._bklass.has_resuming()

    def _create_items_generator(self, backend_args, archive_args):
        """Create a Perceval items generator.

        This method will create a items generator using the
        internal backend defined for this job and the given
        parameters.

        :param backend_args: arguments to execute the backend
        :param archive_args: archive arguments

        :returns: a `BackendItemsGenerator` instance
        """
        fetch_archive = archive_args and archive_args['fetch_from_archive']

        if fetch_archive:
            archived_after = archive_args.get('archived_after', None)
        else:
            archived_after = None

        return perceval.backend.BackendItemsGenerator(self._bklass,
                                                      backend_args,
                                                      self.category,
                                                      manager=self.archive_manager,
                                                      fetch_archive=fetch_archive,
                                                      archived_after=archived_after)

    def _metadata(self, item):
        """Add metadata to an item.

        Method that adds in place metadata to Perceval items such as
        the identifier of the job that generated it or the version of
        the system.

        :param item: an item generated by Perceval
        """
        item['arthur_version'] = __version__
        item['job_id'] = self.job_id


def execute_perceval_job(backend, backend_args, qitems, task_id, category,
                         archive_args=None):
    """Execute a Perceval job on RQ.

    The items fetched during the process will be stored in a
    Redis queue named `queue`.

    Setting the parameter `archive_path`, raw data will be stored
    with the archive manager. The contents from the archive can
    be retrieved setting the parameter `fetch_from_archive` to `True`,
    too. Take into account this behaviour will be only available
    when the backend supports the use of the archive. If archiving
    is not supported, an `AttributeError` exception will be raised.

    :param backend: backend to execute
    :param backend_args: dict of arguments for running the backend
    :param qitems: name of the RQ queue used to store the items
    :param task_id: identifier of the task linked to this job
    :param category: category of the items to retrieve
    :param archive_args: archive arguments

    :returns: a `JobResult` instance

    :raises NotFoundError: raised when the backend is not found
    :raises AttributeError: raised when archiving is not supported but
        any of the archive parameters were set
    """
    rq_job = rq.get_current_job()

    job = PercevalJob(rq_job.id, task_id, backend, category,
                      rq_job.connection, qitems)

    logger.debug("Running job #%s (task: %s) (%s) (cat:%s)",
                 job.job_id, task_id, backend, category)

    if not job.has_archiving() and archive_args:
        raise AttributeError("archive attributes set but archive is not supported")

    try:
        job.run(backend_args, archive_args=archive_args)
    except AttributeError as e:
        raise e
    except Exception as e:
        rq_job = rq.get_current_job()
        rq_job.meta['result'] = job.result
        rq_job.save_meta()
        logger.debug("Error running job %s (%s) - %s",
                     job.job_id, backend, str(e))
        raise e

    result = job.result

    logger.debug("Job #%s (task: %s) completed (%s) - %s/%s items (%s) fetched",
                 result.job_id, task_id, result.backend,
                 str(result.summary.fetched), str(result.summary.skipped),
                 result.category)

    return result
