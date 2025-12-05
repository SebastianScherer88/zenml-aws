import pulumi
import pulumi_aws as aws


class NetworkConfig:
    public: bool = True


class NetworkStack(pulumi.ComponentResource):
    name: str = "zenml-aws-stack"

    config: NetworkConfig
    vpc: aws.ec2.Vpc
    security_group: aws.ec2.SecurityGroup
    subnets: aws.ec2.AwaitableGetSubnetsResult

    def __init__(
        self,
        config: NetworkConfig,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__(
            "zenml-aws:components:Network",
            self.name,
            opts,
        )

        self.config = config

        self.vpc = aws.ec2.get_vpc(default=True)
        self.security_group = aws.ec2.SecurityGroup(
            "zenml-sg",
            description="Security group for ZenML server and metadata store",
            vpc_id=self.vpc.id,  # must be the VPC where ECS & RDS are
            opts=pulumi.ResourceOptions(parent=self),
        )
        aws.ec2.SecurityGroupRule(
            "zenml-sg-egress-all",
            type="egress",
            from_port=0,
            to_port=0,
            protocol="-1",
            security_group_id=self.security_group.id,
            cidr_blocks=["0.0.0.0/0"],
            description="Allow all outbound traffic",
            opts=pulumi.ResourceOptions(parent=self),
        )
        aws.ec2.SecurityGroupRule(
            "zenml-sg-ingress-self",
            type="ingress",
            from_port=0,
            to_port=0,
            protocol="-1",
            security_group_id=self.security_group.id,
            source_security_group_id=self.security_group.id,
            description="Allow all inbound from this same security group",
            opts=pulumi.ResourceOptions(parent=self),
        )
        sn_public = aws.ec2.get_subnets(
            filters=[
                aws.ec2.GetSubnetsFilterArgs(
                    name="vpc-id",
                    values=[self.vpc.id],
                ),
                aws.ec2.GetSubnetsFilterArgs(
                    name="map-public-ip-on-launch",
                    values=["true"],
                ),
            ]
        )

        sn_private = aws.ec2.get_subnets(
            filters=[
                aws.ec2.GetSubnetsFilterArgs(
                    name="vpc-id",
                    values=[self.vpc.id],
                ),
                aws.ec2.GetSubnetsFilterArgs(
                    name="map-public-ip-on-launch",
                    values=["false"],
                ),
            ]
        )

        if self.config.public:
            self.subnets = sn_public
        else:
            self.subnets = sn_private
