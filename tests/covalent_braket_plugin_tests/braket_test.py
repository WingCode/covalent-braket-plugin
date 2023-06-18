# Copyright 2021 Agnostiq Inc.
#
# This file is part of Covalent.
#
# Licensed under the GNU Affero General Public License 3.0 (the "License").
# A copy of the License may be obtained with this software package or at
#
#      https://www.gnu.org/licenses/agpl-3.0.en.html
#
# Use of this file is prohibited except in compliance with the License. Any
# modifications or derivative works of this file must retain this copyright
# notice, and modified files must contain a notice indicating that they have
# been altered from the originals.
#
# Covalent is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the License for more details.
#
# Relief from the License may be granted by purchasing a commercial license.

"""Unit tests for AWS batch executor."""

import asyncio
import os
from base64 import b64encode
from typing import Dict, List
from unittest.mock import AsyncMock, MagicMock

import cloudpickle
import pytest
from boto3.exceptions import Boto3Error

from covalent_braket_plugin.braket import BraketExecutor

MOCK_CREDENTIALS = "mock_credentials"
MOCK_PROFILE = "mock_profile"
MOCK_S3_BUCKET_NAME = "mock_s3_bucket_name"
MOCK_BRAKET_JOB_EXECUTION_ROLE_NAME = "mock_role_name"
MOCK_QUANTUM_DEVICE = "mock_device"
MOCK_CLASSICAL_DEVICE = "mock_device"
MOCK_STORAGE = 1
MOCK_TIME_LIMIT = 1
MOCK_POLL_FREQ = 1


@pytest.fixture
def braket_executor(mocker):
    config_mock = mocker.patch("covalent_braket_plugin.braket.get_config")
    config_mock.return_value = "default"
    return BraketExecutor(
        credentials=MOCK_CREDENTIALS,
        profile=MOCK_PROFILE,
        s3_bucket_name=MOCK_S3_BUCKET_NAME,
        braket_job_execution_role_name=MOCK_BRAKET_JOB_EXECUTION_ROLE_NAME,
        quantum_device=MOCK_QUANTUM_DEVICE,
        classical_device=MOCK_CLASSICAL_DEVICE,
        storage=MOCK_STORAGE,
        time_limit=MOCK_TIME_LIMIT,
        poll_freq=MOCK_POLL_FREQ,
    )


def test_executor_init_default_values(braket_executor):
    """Test that the init values of the executor are set properly."""
    assert braket_executor.credentials_file == MOCK_CREDENTIALS
    assert braket_executor.profile == MOCK_PROFILE
    assert braket_executor.s3_bucket_name == MOCK_S3_BUCKET_NAME
    assert braket_executor.execution_role == MOCK_BRAKET_JOB_EXECUTION_ROLE_NAME
    assert braket_executor.quantum_device == MOCK_QUANTUM_DEVICE
    assert braket_executor.classical_device == MOCK_CLASSICAL_DEVICE
    assert braket_executor.storage == MOCK_STORAGE
    assert braket_executor.time_limit == MOCK_TIME_LIMIT
    assert braket_executor.poll_freq == MOCK_POLL_FREQ


@pytest.mark.asyncio
async def test_run(braket_executor, mocker):
    """Test the run method."""

    asyncmock = AsyncMock()

    def mock_func(x):
        return x

    task_metadata = {"dispatch_id": "mock_dispatch_id", "node_id": 1, "results_dir": "/tmp"}

    mocker.patch("covalent_braket_plugin.braket.boto3")
    validate_creds_mock = mocker.patch(
        "covalent_braket_plugin.braket.BraketExecutor._validate_credentials"
    )
    upload_task_mock = mocker.patch(
        "covalent_braket_plugin.braket.BraketExecutor._upload_task", return_value=asyncmock
    )
    submit_task_mock = mocker.patch(
        "covalent_braket_plugin.braket.BraketExecutor.submit_task", return_value=asyncmock
    )
    poll_task_mock = mocker.patch(
        "covalent_braket_plugin.braket.BraketExecutor._poll_task", return_value=asyncmock
    )
    query_result_async_mock = AsyncMock()
    query_result_mock = mocker.patch(
        "covalent_braket_plugin.braket.BraketExecutor.query_result",
        return_value=query_result_async_mock,
    )
    query_result_mock.return_value = "result", "", ""
    braket_executor.get_cancel_requested = AsyncMock(return_value=False)

    await braket_executor.run(
        function=mock_func, args=[], kwargs={"x": 1}, task_metadata=task_metadata
    )

    validate_creds_mock.assert_called_once()
    upload_task_mock.assert_called_once_with(
        mock_func, [], {"x": 1}, {"image_tag": "mock_dispatch_id-1"}
    )
    submit_task_mock.assert_called_once()


@pytest.mark.asyncio
async def test_submit_task(braket_executor, mocker):
    boto3_mock = mocker.patch("covalent_braket_plugin.braket.boto3")

    submit_metadata = {
        "image_tag": "mock-image-tag",
        "result_filename": "mock_filename.pkl",
        "account": 122388,
    }
    await braket_executor.submit_task(submit_metadata)

    boto3_mock.Session().client().create_job.assert_called_once()


@pytest.mark.asyncio
async def test_upload_task(braket_executor, mocker):

    """Test the package and upload method."""
    boto3_mock = mocker.patch("covalent_braket_plugin.braket.boto3")

    await braket_executor._upload_task(
        "mock_transportable_object",
        [],
        {},
        {"image_tag": "mock_image_tag"},
    )
    boto3_mock.Session().client().upload_file.assert_called_once()


@pytest.mark.asyncio
async def test_get_status(braket_executor):
    """Test the get status method."""

    class MockBraket:
        def get_job(self, jobArn: str) -> Dict:
            if jobArn == "1":
                return {"status": "SUCCESS"}
            elif jobArn == "2":
                return {"status": "RUNNING"}

    status = await braket_executor.get_status(braket=MockBraket(), job_arn="1")
    assert status == "SUCCESS"

    status = await braket_executor.get_status(braket=MockBraket(), job_arn="2")
    assert status == "RUNNING"


@pytest.mark.asyncio
async def test_poll_braket_job(braket_executor, mocker):
    """Test the method to poll the batch job."""

    async_mock = AsyncMock(side_effect=["QUEUED", "FAILED"])
    boto3_mock = mocker.patch("covalent_braket_plugin.braket.boto3")
    get_status_mock = mocker.patch(
        "covalent_braket_plugin.braket.BraketExecutor.get_status", side_effect=async_mock
    )

    boto3_mock.Session().client().get_job.return_value = {"failureReason": "error"}
    with pytest.raises(Exception):
        await braket_executor._poll_task({"job_arn": 1})
    get_status_mock.assert_awaited()


@pytest.mark.asyncio
async def test_query_result(braket_executor, mocker):
    """Test the method to query the results."""

    def download_file(filename, bucket_name, func_filename):
        return filename

    def describe_log_streams(logGroupName, logStreamNamePrefix):
        print("******DESCRIBE LOG STREAMS********")
        print(logGroupName)
        print(logStreamNamePrefix)
        return {"logStreams": [{"logStreamName": f"{logStreamNamePrefix}-mock-name"}]}

    def get_log_events(logGroupName, logStreamName):
        return {"events": [{"message": "mock_logs"}]}

    boto3_mock = mocker.patch("covalent_braket_plugin.braket.boto3")
    boto3_client_mock = boto3_mock.Session().client()

    boto3_client_mock.download_file.side_effect = download_file
    boto3_client_mock.describe_log_streams.side_effect = describe_log_streams
    boto3_client_mock.get_log_events.side_effect = get_log_events

    task_results_dir, result_filename = "/tmp", "mock_result_filename.pkl"
    local_result_filename = os.path.join(task_results_dir, result_filename)
    with open(local_result_filename, "wb") as f:
        cloudpickle.dump("hello world", f)

    query_metadata = {
        "result_filename": result_filename,
        "task_results_dir": task_results_dir,
        "image_tag": "1",
    }
    assert await braket_executor.query_result(query_metadata) == (
        "hello world",
        "mock_logs\n",
        "",
    )


@pytest.mark.asyncio
async def test_cancel_braket_task(braket_executor, mocker):
    boto3_mock = mocker.patch("covalent_braket_plugin.braket.boto3")
    boto3_client_mock = boto3_mock.Session().client()
    boto3_client_mock.cancel_quantum_task.return_value = {"status": "CANCELLED"}
    mock_arn = (
        "arn:aws:braket:us-west-2:123456789012:quantum-task/01234567-89ab-cdef-0123-456789abcdef"
    )
    mock_dispatch_id = "abcdef"
    mock_node_id = 0
    mock_task_metadata = {"dispatch_id": mock_dispatch_id, "node_id": mock_node_id}

    is_cancelled = await braket_executor.cancel(task_metadata=mock_task_metadata,
                                          job_handle=mock_arn)

    assert is_cancelled is True
    assert boto3_client_mock.cancel_quantum_task.called_once_with(quantumTaskArn=mock_arn)


@pytest.mark.asyncio
async def test_cancel_failed_braket_task(braket_executor, mocker):
    boto3_mock = mocker.patch("covalent_braket_plugin.braket.boto3")
    boto3_client_mock = boto3_mock.Session().client()
    boto3_client_mock.cancel_quantum_task.return_value = {"status": "CANCELLED"}
    mock_arn = (
        "arn:aws:braket:us-west-2:123456789012:quantum-task/01234567-89ab-cdef-0123-456789abcdef"
    )
    mock_dispatch_id = "abcdef"
    mock_node_id = 0
    mock_task_metadata = {"dispatch_id": mock_dispatch_id, "node_id": mock_node_id}
    mock_error = Boto3Error(
        'Could not connect to the endpoint URL: \
            "https://braket.us-east-1.amazonaws.com/v1/quantum-task/"'
        )
    boto3_client_mock.cancel_quantum_task.side_effect = mock_error

    with pytest.raises(Boto3Error) as exception:
        is_cancelled = await braket_executor.cancel(task_metadata=mock_task_metadata,
                                                    job_handle=mock_arn)
        assert (
                f"Failed to cancel Braket quantum task with task metadata: \
                {mock_task_metadata} and error:{mock_error}"
                == exception
            )
        assert is_cancelled is False
    assert boto3_client_mock.cancel_quantum_task.called_once_with(mock_arn)
