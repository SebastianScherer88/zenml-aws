"""An AWS Python Pulumi program"""

import pulumi
import pulumi_aws as aws
from pulumi.resource import ComponentResource
from pydantic import BaseModel


class ZenMLContainerRegistryConfig(BaseModel):
    force_delete: bool = True


class ZenMLArtifactStoreConfig(BaseModel):
    force_destroy: bool = True


class ZenMLRemoteStackConfig(BaseModel):
    container_registry: ZenMLContainerRegistryConfig = ZenMLContainerRegistryConfig()
    artifact_store: ZenMLArtifactStoreConfig = ZenMLArtifactStoreConfig()


class ZenMLRemoteStack(ComponentResource):
    name: str = "zenml-remote-stack"

    config: ZenMLRemoteStackConfig
    container_registry: aws.ecr.Repository
    artifact_store: aws.s3.Bucket

    def __init__(
        self,
        config: ZenMLRemoteStackConfig,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__(
            "zenml-aws:components:ZenMLRemoteStack",
            self.name,
            None,
            opts,
        )

        self.config = config
        self.create_artifact_store()
        self.create_container_registry()

    def create_artifact_store(self):
        self.artifact_store = aws.s3.Bucket(
            "zenml-artifact-store",
            force_destroy=self.config.artifact_store.force_destroy,
            opts=pulumi.ResourceOptions(parent=self),
        )

    def create_container_registry(self):
        self.container_registry = aws.ecr.Repository(
            "zenml-container-registry",
            force_delete=self.config.container_registry.force_delete,
            opts=pulumi.ResourceOptions(parent=self),
        )

    def export_outputs(self):
        pulumi.export("zenml-artifact-store-bucket-arn", self.artifact_store.arn)
        pulumi.export("zenml-artifact-store-bucket-name", self.artifact_store.bucket)
        pulumi.export("zenml-container-registry-arn", self.container_registry.arn)
        pulumi.export("zenml-container-registry-name", self.container_registry.name)
