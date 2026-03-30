.PHONY: build-agent clean-agent help

IMAGE_NAME ?= corpclaw-agent-base
IMAGE_TAG  ?= latest

## build-agent: Build the Docker sandbox image for agent execution
build-agent:
	@echo "Building $(IMAGE_NAME):$(IMAGE_TAG)..."
	docker build \
		--file docker/Dockerfile \
		--tag $(IMAGE_NAME):$(IMAGE_TAG) \
		.
	@echo "Done: $(IMAGE_NAME):$(IMAGE_TAG)"

## clean-agent: Remove the sandbox image
clean-agent:
	docker rmi $(IMAGE_NAME):$(IMAGE_TAG) || true

## help: Show available make targets
help:
	@grep -E '^##' Makefile | sed 's/## /  /'
