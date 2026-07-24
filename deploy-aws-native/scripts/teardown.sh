#!/usr/bin/env bash
# Delete the whole AWS stack when the pilot ends. Your DATA SURVIVES:
#   • RDS  -> a FINAL SNAPSHOT is kept (DeletionPolicy: Snapshot)
#   • S3   -> the bucket is RETAINED (DeletionPolicy: Retain)
#   • EC2, IAM role, security groups -> removed
# Run from your LAPTOP (needs AWS CLI).
#   deploy-aws-native/scripts/teardown.sh
set -euo pipefail
STACK="${STACK:-ajace-timesheet}"

echo "About to DELETE CloudFormation stack: $STACK"
echo "  EC2 app host + security groups + IAM role  ->  REMOVED"
echo "  RDS database                                ->  FINAL SNAPSHOT kept, instance removed"
echo "  S3 bucket (uploaded files)                  ->  RETAINED"
echo
read -r -p "Type the stack name ('$STACK') to confirm: " CONFIRM
[ "$CONFIRM" = "$STACK" ] || { echo "aborted."; exit 1; }

aws cloudformation delete-stack --stack-name "$STACK"
echo "Delete requested. Track it with:"
echo "  aws cloudformation wait stack-delete-complete --stack-name $STACK"
echo
echo "Kept afterward (tiny storage cost ~\$1/mo): the RDS final snapshot + the S3 bucket."
echo "To come back later: re-deploy the stack, then restore the DB from the snapshot"
echo "(console: RDS > Snapshots > Restore) or point a new DBInstance at it."
