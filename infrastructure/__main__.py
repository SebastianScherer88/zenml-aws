from network import NetworkConfig, NetworkStack
from zenml_deployment import ZenMLDeployment, ZenMLDeploymentConfig
from zenml_stack import ZenMLAWSStack, ZenMLAWSStackConfig

network_config = NetworkConfig()
network_stack = NetworkStack(config=network_config)

zenml_config = ZenMLAWSStackConfig()
zenml_stack = ZenMLAWSStack(config=zenml_config, network_stack=network_stack)
zenml_stack.export_outputs()

deployment_config = ZenMLDeploymentConfig()
deployment = ZenMLDeployment(
    config=deployment_config, network_stack=network_stack, zenml_stack=zenml_stack
)
deployment.export_outputs()
