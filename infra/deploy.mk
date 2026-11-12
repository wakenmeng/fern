# Deploy targets for the 72-hour live window (docs/DEPLOY.md). Included from the Makefile.
# Needs: aws CLI (logged in), tofu (or terraform), docker buildx, kubectl.
# kubectl always uses infra/.kubeconfig, never your default context.

AWS_REGION ?= us-west-2
RUN_ID     ?= live-1
TF         ?= tofu
TFD        := $(TF) -chdir=infra/aws
TFVARS     := -var region=$(AWS_REGION) -var run_id=$(RUN_ID)
KCFG       := infra/.kubeconfig
K          := kubectl --kubeconfig $(KCFG) -n fern
TAG        ?= $(shell git rev-parse --short HEAD)
IMG         = $$($(TFD) output -raw ecr_repository_url):$(TAG)

.PHONY: infra-up infra-down kubeconfig image k8s-secret k8s-migrate k8s-apply k8s-pool deploy k8s-status

infra-up:  ## VPC, EKS, RDS, Redis, ECR (about 15-20 minutes)
	$(TFD) init -input=false
	$(TFD) apply $(TFVARS)

infra-down:  ## destroy everything this run created
	-$(K) delete namespace fern --wait=true
	$(TFD) destroy $(TFVARS)

kubeconfig:
	aws eks update-kubeconfig --region $(AWS_REGION) --name $$($(TFD) output -raw cluster_name) --kubeconfig $(KCFG)

image:  ## build for Graviton (arm64) and push to ECR
	@REPO=$$($(TFD) output -raw ecr_repository_url); \
	aws ecr get-login-password --region $(AWS_REGION) | docker login --username AWS --password-stdin $${REPO%%/*}; \
	docker buildx build --platform linux/arm64 -t $$REPO:$(TAG) --push .

# Secrets: .env.deploy (gitignored, see .env.deploy.example) plus the Terraform outputs.
# The combined env file only exists inside a private temp dir for the duration of this target.
k8s-secret: kubeconfig
	@test -f .env.deploy || (echo "missing .env.deploy (copy .env.deploy.example)"; exit 1)
	kubectl --kubeconfig $(KCFG) apply -f infra/k8s/namespace.yaml
	@umask 077; dir=$$(mktemp -d); \
	grep '=' .env.deploy | grep -v '^#' | grep -v '^CLOUDFLARE_TUNNEL_TOKEN=' > "$$dir/env"; \
	echo "FERN_DATABASE_URL=$$($(TFD) output -raw database_url)" >> "$$dir/env"; \
	echo "FERN_REDIS_URL=$$($(TFD) output -raw redis_url)" >> "$$dir/env"; \
	$(K) create secret generic fern-env --from-env-file="$$dir/env" --dry-run=client -o yaml | $(K) apply -f -; \
	rm -f "$$dir/env"; rmdir "$$dir"
	@token=$$(grep '^CLOUDFLARE_TUNNEL_TOKEN=' .env.deploy | cut -d= -f2-); \
	$(K) create secret generic cloudflared --from-literal=token="$$token" --dry-run=client -o yaml | $(K) apply -f -

k8s-migrate:
	$(K) delete job fern-migrate --ignore-not-found
	sed "s#image: IMAGE#image: $(IMG)#" infra/k8s/job-migrate.yaml | $(K) apply -f -
	$(K) wait --for=condition=complete job/fern-migrate --timeout=300s

k8s-apply:
	sed "s#image: IMAGE#image: $(IMG)#" infra/k8s/api.yaml | $(K) apply -f -
	$(K) apply -f infra/k8s/cloudflared.yaml
	$(K) rollout status deployment/fern-api --timeout=300s
	$(K) rollout status deployment/cloudflared --timeout=300s

k8s-pool:  ## open and pre-fund 5 demo spaces on Column (about 3 minutes)
	$(K) delete job fern-pool --ignore-not-found
	sed "s#image: IMAGE#image: $(IMG)#" infra/k8s/job-pool.yaml | $(K) apply -f -
	$(K) wait --for=condition=complete job/fern-pool --timeout=600s
	$(K) logs job/fern-pool --tail=3

deploy: image k8s-secret k8s-migrate k8s-apply  ## build, push, migrate, roll out

k8s-status:
	$(K) get pods,jobs,svc
