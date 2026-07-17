import json
import os
import uuid

import boto3
from botocore.client import Config

# Pinning region_name + signature_version alone signs for the right region
# but boto3 still builds the URL against the legacy global s3.amazonaws.com
# endpoint, which S3 then 307-redirects away from - pin endpoint_url too so
# the generated URL's host matches the region it's actually signed for.
REGION = os.environ["AWS_REGION"]
s3 = boto3.client(
    "s3",
    region_name=REGION,
    endpoint_url=f"https://s3.{REGION}.amazonaws.com",
    config=Config(signature_version="s3v4"),
)
BUCKET = os.environ["UPLOADS_BUCKET"]
EXPIRES_IN = 900


def handler(event, context):
    key = f"uploads/{uuid.uuid4()}.stflb"
    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": key, "ContentType": "application/octet-stream"},
        ExpiresIn=EXPIRES_IN,
    )
    return _response(200, {"upload_url": url, "program_key": key, "expires_in": EXPIRES_IN})


def _response(status, payload):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }
