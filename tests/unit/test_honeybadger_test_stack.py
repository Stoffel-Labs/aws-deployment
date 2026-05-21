import aws_cdk as core
import aws_cdk.assertions as assertions

from honeybadger_test.honeybadger_test_stack import HoneybadgerTestStack

# example tests. To run these tests, uncomment this file along with the example
# resource in honeybadger_test/honeybadger_test_stack.py
def test_sqs_queue_created():
    app = core.App()
    stack = HoneybadgerTestStack(app, "honeybadger-test")
    template = assertions.Template.from_stack(stack)

#     template.has_resource_properties("AWS::SQS::Queue", {
#         "VisibilityTimeout": 300
#     })
