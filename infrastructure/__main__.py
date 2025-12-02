from zenml_deployment import ZenMLDeployment, ZenMLDeploymentConfig
from zenml_stack import ZenMLRemoteStack, ZenMLRemoteStackConfig

stack_config = ZenMLRemoteStackConfig()
stack = ZenMLRemoteStack(config=stack_config)

deployment_config = ZenMLDeploymentConfig()
deployment = ZenMLDeployment(config=deployment_config, zenml_remote_stack=stack)
