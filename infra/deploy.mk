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

.PHONY: infra-up infra-down kubeconfig image k8s-secret k8s-migrate k8s-apply k8s-pool deploy k8s-status tunnel bench

infra-up:  ## VPC, EKS, RDS, Redis, ECR (about 15-20 minutes)
	$(TFD) init -input=false
	$(TFD) apply $(TFVARS)

infra-down:  ## destroy everything this run created
	-$(K) delete namespace fern --wait=true
	$(TFD) destroy $(TFVARS)

kubeconfig:
	aws eks update-kubeconfig --region $(AWS_REGION) --name $$($(TFD) output -raw cluster_name) --kubeconfig $(KCFG)

# Builds the committed tree only (git archive), so uncommitted edits never ship under a git-sha tag.
image:  ## build for Graviton (arm64) and push to ECR
	@REPO=$$($(TFD) output -raw ecr_repository_url); \
	aws ecr get-login-password --region $(AWS_REGION) | docker login --username AWS --password-stdin $${REPO%%/*}; \
	git archive $(TAG) | docker buildx build --platform linux/arm64 -t $$REPO:$(TAG) --push -

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

# Load test (about 8 minutes): a sim-bank copy of the API on its own database, driven by
# bench/load.py from inside the cluster. Result: bench/results/latest.json. Everything is removed after.
BENCH_STAGES ?= 30:50,240:200,60:400,30:50
bench:
	@url=$$($(TFD) output -raw database_url | sed 's#/fern?sslmode#/fern_bench?sslmode#'); \
	$(K) create secret generic fern-bench-env --from-literal=FERN_DATABASE_URL="$$url" \
	  --dry-run=client -o yaml | $(K) apply -f - >/dev/null
	$(K) delete job fern-bench-db fern-bench-load --ignore-not-found
	sed "s#image: IMAGE#image: $(IMG)#" infra/k8s/job-bench-db.yaml | $(K) apply -f -
	$(K) wait --for=condition=complete job/fern-bench-db --timeout=300s
	sed "s#image: IMAGE#image: $(IMG)#" infra/k8s/bench-api.yaml | $(K) apply -f -
	$(K) rollout status deployment/fern-bench --timeout=300s
	$(K) create configmap fern-bench-script --from-file=bench/load.py --dry-run=client -o yaml | $(K) apply -f -
	sed -e "s#image: IMAGE#image: $(IMG)#" -e 's#args: \[\]#args: ["--stages", "$(BENCH_STAGES)"]#' \
	  infra/k8s/job-bench-load.yaml | $(K) apply -f -
	$(K) wait --for=condition=complete job/fern-bench-load --timeout=1800s
	@mkdir -p bench/results
	$(K) logs job/fern-bench-load | sed -n '/^BENCH_JSON_BEGIN$$/,/^BENCH_JSON_END$$/p' | sed '1d;$$d' \
	  | python3 -m json.tool > bench/results/latest.json
	@cp bench/results/latest.json bench/results/$$(date -u +%Y%m%dT%H%MZ).json
	$(K) delete deployment/fern-bench service/fern-bench job/fern-bench-load job/fern-bench-db --ignore-not-found
	@python3 -c "import json; r=json.load(open('bench/results/latest.json')); print(r['headline']); print(r['money'])"

# Cloudflare Tunnel via the CLI (after `cloudflared tunnel login`): create "fern" once, point
# fern-origin.waken.dev at it, and store its connector token in .env.deploy.
tunnel:
	cloudflared tunnel info fern >/dev/null 2>&1 || cloudflared tunnel create fern
	cloudflared tunnel route dns --overwrite-dns fern fern-origin.waken.dev
	@token=$$(cloudflared tunnel token fern); \
	grep -v '^CLOUDFLARE_TUNNEL_TOKEN=' .env.deploy > .env.deploy.tmp; \
	echo "CLOUDFLARE_TUNNEL_TOKEN=$$token" >> .env.deploy.tmp; \
	chmod 600 .env.deploy.tmp; mv .env.deploy.tmp .env.deploy; \
	echo "tunnel token written to .env.deploy"
