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

"""AWS Braket Hybrid Jobs executor plugin for the Covalent dispatcher."""

import base64
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import boto3
import cloudpickle as pickle
import docker

from covalent._shared_files.logger import app_log
from covalent._shared_files.util_classes import DispatchInfo
from covalent._workflow.transport import TransportableObject
from covalent.executor import BaseExecutor

_EXECUTOR_PLUGIN_DEFAULTS = {
    "credentials": os.environ.get("AWS_SHARED_CREDENTIALS_FILE")
    or os.path.join(os.environ["HOME"], ".aws/credentials"),
    "profile": os.environ.get("AWS_PROFILE") or "",
    "s3_bucket_name": "amazon-braket-covalent-job-resources",
    "ecr_repo_name": "covalent-braket-job-images",
    "braket_job_execution_role_name": "CovalentBraketJobsExecutionRole",
    "quantum_device": "arn:aws:braket:::device/quantum-simulator/amazon/sv1",
    "classical_device": "ml.m5.large",
    "storage": 30,
    "time_limit": 300,
    "cache_dir": "/tmp/covalent",
    "poll_freq": 30,
}

executor_plugin_name = "BraketExecutor"

class BraketExecutor(BaseExecutor):
    """AWS Braket Hybrid Jobs executor plugin class."""

    def __init__(
        self,
        credentials: str,
        profile: str,
        s3_bucket_name: str,
        ecr_repo_name: str,
        braket_job_execution_role_name: str,
        quantum_device: str,
        classical_device: str,
        storage: int,
        time_limit: int,
        poll_freq: int,
        **kwargs
    ):
        super().__init__(**kwargs)

        self.credentials = credentials
        self.profile = profile
        self.s3_bucket_name = s3_bucket_name
        self.ecr_repo_name = ecr_repo_name
        self.braket_job_execution_role_name = braket_job_execution_role_name
        self.quantum_device = quantum_device
        self.classical_device = classical_device
        self.storage = storage
        self.time_limit = time_limit
        self.poll_freq = poll_freq

    def execute(
        self,
        function: TransportableObject,
        args: List,
        kwargs: Dict,
        dispatch_id: str,
        results_dir: str,
        node_id: int = -1,
    ) -> Tuple[Any, str, str]:

        dispatch_info = DispatchInfo(dispatch_id)
        result_filename = f"result-{dispatch_id}-{node_id}.pkl"
        task_results_dir = os.path.join(results_dir, dispatch_id)
        image_tag = f"{dispatch_id}-{node_id}"

        # AWS Credentials
        os.environ["AWS_SHARED_CREDENTIALS_FILE"] = self.credentials
        os.environ["AWS_PROFILE"] = self.profile

        # AWS Account Retrieval
        sts = boto3.client("sts")
        identity = sts.get_caller_identity()
        account = identity.get("Account")

        if account is None:
            app_log.warning(identity)
            return None, "", identity

        # TODO: Move this to BaseExecutor
        Path(self.cache_dir).mkdir(parents=True, exist_ok=True)

        with self.get_dispatch_context(dispatch_info):
            ecr_repo_uri = self._package_and_upload(
                function,
                image_tag,
                task_results_dir,
                result_filename,
                args,
                kwargs,
            )

            braket = boto3.client("braket")

            job = braket.create_job(
                algorithmSpecification={
                    "containerImage": {
                        "uri": ecr_repo_uri,
                    },
                },
                checkpointConfig={
                    "s3Uri": f"s3://{self.s3_bucket_name}/checkpoints/{image_tag}",
                },
                deviceConfig={
                    "device": self.quantum_device,
                },
                instanceConfig={
                    "instanceType": self.classical_device,
                    "volumeSizeInGb": self.storage,
                },
                jobName=f"covalent-{image_tag}",
                outputDataConfig={
                    "s3Path": f"s3://{self.s3_bucket_name}/braket/{image_tag}",
                },
                roleArn=f"arn:aws:iam::{account}:role/{self.braket_job_execution_role_name}",
                stoppingCondition={
                    "maxRuntimeInSeconds": self.time_limit,
                },
            )

            job_arn = job["jobArn"]

            self._poll_braket_job(braket, job_arn)

            return self._query_result(result_filename, task_results_dir, job_arn, image_tag)

    def _format_exec_script(
        self,
        func_filename: str,
        result_filename: str,
        docker_working_dir: str,
        args: List,
        kwargs: Dict,
    ) -> str:
        """Create an executable Python script which executes the task.

        Args:
            func_filename: Name of the pickled function.
            result_filename: Name of the pickled result.
            docker_working_dir: Name of the working directory in the container.
            args: Positional arguments consumed by the task.
            kwargs: Keyword arguments consumed by the task.

        Returns:
            script: String object containing the executable Python script.
        """

        exec_script = """
import os
import boto3
import cloudpickle as pickle

local_func_filename = os.path.join("{docker_working_dir}", "{func_filename}")
local_result_filename = os.path.join("{docker_working_dir}", "{result_filename}")

s3 = boto3.client("s3")
s3.download_file("{s3_bucket_name}", "{func_filename}", local_func_filename)

with open(local_func_filename, "rb") as f:
    function = pickle.load(f)

result = function(*{args}, **{kwargs})

with open(local_result_filename, "wb") as f:
    pickle.dump(result, f)

s3.upload_file(local_result_filename, "{s3_bucket_name}", "{result_filename}")
""".format(
            func_filename=func_filename,
            args=args,
            kwargs=kwargs,
            s3_bucket_name=self.s3_bucket_name,
            result_filename=result_filename,
            docker_working_dir=docker_working_dir,
        )

        return exec_script

    def _format_dockerfile(self, exec_script_filename: str, docker_working_dir: str) -> str:
        """Create a Dockerfile which wraps an executable Python task.
        
        Args:
            exec_script_filename: Name of the executable Python script.
            docker_working_dir: Name of the working directory in the container.

        Returns:
            dockerfile: String object containing a Dockerfile.
        """

        dockerfile = """
FROM python:3.8-slim-buster

RUN apt-get update && apt-get install -y \\
  gcc \\
  && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --use-feature=in-tree-build --upgrade \\
  amazon-braket-pennylane-plugin \\
  boto3==1.20.48 \\
  cloudpickle==2.0.0 \\
  pennylane==0.16.0 \\
  sagemaker-training

WORKDIR {docker_working_dir}

COPY {func_basename} {docker_working_dir}

ENV SAGEMAKER_PROGRAM {func_basename}
""".format(
            func_basename=os.path.basename(exec_script_filename),
            docker_working_dir=docker_working_dir,
        )

        return dockerfile

    def _package_and_upload(
        self,
        function: TransportableObject,
        image_tag: str,
        task_results_dir: str,
        result_filename: str,
        args: List,
        kwargs: Dict,
    ) -> str:
        """Package a task using Docker and upload it to AWS ECR.
        
        Args:
            function: A callable Python function.
            image_tag: Tag used to identify the Docker image.
            task_results_dir: Local directory where task results are stored.
            result_filename: Name of the pickled result.
            args: Positional arguments consumed by the task.
            kwargs: Keyword arguments consumed by the task.

        Returns:
            ecr_repo_uri: URI of the repository where the image was uploaded.
        """

        func_filename = f"func-{image_tag}.pkl"
        docker_working_dir = "/opt/ml/code"

        with tempfile.NamedTemporaryFile(dir=self.cache_dir) as function_file:
            # Write serialized function to file
            pickle.dump(function.get_deserialized(), function_file)
            function_file.flush()

            # Upload pickled function to S3
            s3 = boto3.client("s3")
            s3.upload_file(function_file.name, self.s3_bucket_name, func_filename)

        with tempfile.NamedTemporaryFile(
            dir=self.cache_dir, mode="w", suffix=".py"
        ) as exec_script_file, tempfile.NamedTemporaryFile(
            dir=self.cache_dir, mode="w"
        ) as dockerfile_file:
            # Write execution script to file
            exec_script = self._format_exec_script(
                func_filename,
                result_filename,
                docker_working_dir,
                args,
                kwargs,
            )
            exec_script_file.write(exec_script)
            exec_script_file.flush()

            # Write Dockerfile to file
            dockerfile = self._format_dockerfile(exec_script_file.name, docker_working_dir)
            dockerfile_file.write(dockerfile)
            dockerfile_file.flush()

            local_dockerfile = os.path.join(task_results_dir, f"Dockerfile_{image_tag}")
            shutil.copyfile(dockerfile_file.name, local_dockerfile)

            # Build the Docker image
            docker_client = docker.from_env()
            image, build_log = docker_client.images.build(
                path=self.cache_dir, dockerfile=dockerfile_file.name, tag=image_tag
            )

        # ECR config
        ecr = boto3.client("ecr")

        ecr_username = "AWS"
        ecr_credentials = ecr.get_authorization_token()["authorizationData"][0]
        ecr_password = (
            base64.b64decode(ecr_credentials["authorizationToken"])
            .replace(b"AWS:", b"")
            .decode("utf-8")
        )
        ecr_registry = ecr_credentials["proxyEndpoint"]
        ecr_repo_uri = f"{ecr_registry.replace('https://', '')}/{self.ecr_repo_name}:{image_tag}"

        docker_client.login(username=ecr_username, password=ecr_password, registry=ecr_registry)

        # Tag the image
        image.tag(ecr_repo_uri, tag=image_tag)

        # Push to ECR
        response = docker_client.images.push(ecr_repo_uri, tag=image_tag)

        return ecr_repo_uri

    def get_status(self, braket, job_arn: str) -> str:
        """Query the status of a previously submitted Braket hybrid job.

        Args:
            braket: Braket client object.
            job_arn: ARN used to identify a Braket hybrid job.

        Returns:
            status: String describing the job status.
        """

        job = braket.get_job(jobArn=job_arn)
        status = job["status"]

        return status

    def _poll_braket_job(self, braket, job_arn: str) -> None:
        """Poll a Braket hybrid job until completion.

        Args:
            braket: Braket client object.
            job_arn: ARN used to identify a Braket hybrid job.

        Returns:
            None
        """

        status = self.get_status(braket, job_arn)

        while status not in ["COMPLETED", "FAILED", "CANCELLED"]:
            time.sleep(self.poll_freq)
            status = self.get_status(braket, job_arn)

        if status == "FAILED":
            failure_reason = braket.get_job(jobArn=job_arn)["failureReason"]
            raise Exception(failure_reason)

    def _query_result(
        self,
        result_filename: str,
        task_results_dir: str,
        job_arn: str,
        image_tag: str,
    ) -> Tuple[Any, str, str]:
        """Query and retrieve a completed job's result.

        Args:
            result_filename: Name of the pickled result file.
            task_results_dir: Local directory where task results are stored.
            job_arn: Identifier used to identify a Braket hybrid job.
            image_tag: Tag used to identify the log file.

        Returns:
            result: The task's result, as a Python object.
            logs: The stdout and stderr streams corresponding to the task.
            empty_string: A placeholder empty string.
        """

        local_result_filename = os.path.join(task_results_dir, result_filename)

        s3 = boto3.client("s3")
        s3.download_file(self.s3_bucket_name, result_filename, local_result_filename)

        with open(local_result_filename, "rb") as f:
            result = pickle.load(f)
        os.remove(local_result_filename)

        logs = boto3.client("logs")

        log_group_name = "/aws/braket/jobs"
        log_stream_prefix = "covalent-{image_tag}"
        log_stream_name = logs.describe_log_streams(
            logGroupName=log_group_name,
            logStreamNamePrefix=log_stream_prefix
        )["logStreams"][0]["logStreamName"]

        # TODO: This should be paginated, but the command doesn't support boto3 pagination
        # Up to 10000 log events can be returned from a single call to get_log_events()
        events = logs.get_log_events(
            logGroupName=log_group_name,
            logStreamName=log_stream_name,
        )["events"]

        log_events = ""
        for event in events:
            log_events += event["message"] + "\n"

        return result, log_events, ""

