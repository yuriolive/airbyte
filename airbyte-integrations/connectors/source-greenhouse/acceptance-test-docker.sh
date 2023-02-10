#!/usr/bin/env sh

set -e

ROOT_DIR="$(git rev-parse --show-toplevel)"
CDK_DIR="$ROOT_DIR/airbyte-cdk/python"
CONNECTOR_TAG="$(grep connector_image acceptance-test-config.yml | head -n 1 | cut -d: -f2 | sed 's/^ *//')"
CONNECTOR_NAME="$(echo $CONNECTOR_TAG | cut -d / -f 2)"
CONNECTOR_DIR="$ROOT_DIR/airbyte-integrations/connectors/$CONNECTOR_NAME"
BUILD_DIR=$(mktemp -d)

if [ -n "$LOCAL_CDK" ]; then
  rm -rf "$BUILD_DIR"

  # Copy the CDK & connector files to the build directory
  rsync -a "$CONNECTOR_DIR/" "$BUILD_DIR" --exclude "$BUILD_DIR"

  cd "$BUILD_DIR"
  cp -r "$CDK_DIR" airbyte-cdk

  # Insert an instruction to the Dockerfile to copy the local CDK
  awk 'NR==1 {print; print "\nCOPY airbyte-cdk /airbyte-cdk"} NR!=1' Dockerfile > Dockerfile.copy
  mv Dockerfile.copy Dockerfile

  # Modify setup.py so it uses the local CDK
  sed -iE 's,"airbyte-cdk[^"]*","airbyte-cdk @ file://localhost/airbyte-cdk",' setup.py

  # Build the connector image
  docker build . -t "$CONNECTOR_TAG"
  cd -

  # Clean up now that the image has been created
  rm -rf "$BUILD_DIR"
else
  # Build latest connector image
  docker build . -t "$CONNECTOR_TAG"
fi

# Pull latest acctest image
docker pull airbyte/connector-acceptance-test:latest

# Run
docker run --rm -it \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v /tmp:/tmp \
    -v "$CONNECTOR_DIR":/test_input \
    airbyte/connector-acceptance-test \
    --acceptance-test-config /test_input

