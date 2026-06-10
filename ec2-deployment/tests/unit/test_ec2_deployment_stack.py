import aws_cdk as core
import aws_cdk.assertions as assertions

from ec2_deployment.ec2_deployment_stack import Ec2DeploymentStack

# example tests. To run these tests, uncomment this file along with the example
# resource in ec2_deployment/ec2_deployment_stack.py
def test_sqs_queue_created():
    app = core.App()
    stack = Ec2DeploymentStack(app, "ec2-deployment")
    template = assertions.Template.from_stack(stack)

#     template.has_resource_properties("AWS::SQS::Queue", {
#         "VisibilityTimeout": 300
#     })
