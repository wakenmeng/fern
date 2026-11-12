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

.PHONY: infra-up infra-down kubeconfig image k8s-secret k8s-migrate k8s-apply k8s-pool deploy k8s-status tunnel bench bench-up bench-down archive worker

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

# Load test: a sim-bank copy of the API on its own database, driven by bench/load.py from inside
# the cluster. Result: bench/results/latest.json. Everything is removed after.
#   make bench                  small: fern_bench on the live RDS instance, live nodes (~6 minutes)
#   make bench-up; make bench BENCH_SIZE=large; make bench-down
#                               large: its own Multi-AZ db.m7g.2xlarge and 2 c7g.4xlarge nodes
#                               (infra/aws-bench, about $2.50/hour while up)
BENCH_SIZE ?= small
ifeq ($(BENCH_SIZE),large)
BENCH_STAGES   ?= 60:100,60:200,60:400,60:600,60:800
BENCH_REPLICAS ?= 12
BENCH_API_CPU  ?= 1000m
BENCH_PROCS    ?= 8
BENCH_LOAD_CPU ?= 7000m
BENCH_DB_URL    = $$($(TF) -chdir=infra/aws-bench output -raw database_url)
BENCH_DB_DESC  ?= its own RDS Postgres 16, db.m7g.2xlarge (8 vCPU) Multi-AZ, like live
else
BENCH_DB_DESC  ?= the same RDS instance (db.t4g.micro Multi-AZ), separate database fern_bench
BENCH_STAGES   ?= 60:25,60:50,60:100,60:150,60:200
BENCH_REPLICAS ?= 2
BENCH_API_CPU  ?= 250m
BENCH_PROCS    ?= 4
BENCH_LOAD_CPU ?= 1000m
BENCH_DB_URL    = $$($(TFD) output -raw database_url | sed 's|/fern?sslmode|/fern_bench?sslmode|')
endif
# The bench uses the image fern-api is running now, so it measures exactly what is live
# (override with BENCH_TAG=<git sha> to measure a build before it goes live).
ifdef BENCH_TAG
BENCH_IMG   = $$($(TFD) output -raw ecr_repository_url):$(BENCH_TAG)
else
BENCH_IMG   = $$($(K) get deployment fern-api -o jsonpath='{.spec.template.spec.containers[0].image}')
endif
BENCH_TFD   = $(TF) -chdir=infra/aws-bench
BENCH_TFVARS = -var region=$(AWS_REGION) -var run_id=$(RUN_ID)

bench-up:  ## bench-only database + nodes next to the live run (about 20 minutes)
	$(BENCH_TFD) init -input=false
	$(BENCH_TFD) apply $(BENCH_TFVARS)

bench-down:
	$(BENCH_TFD) destroy $(BENCH_TFVARS)

bench:
	@url=$(BENCH_DB_URL); \
	$(K) create secret generic fern-bench-env --from-literal=FERN_DATABASE_URL="$$url" \
	  --dry-run=client -o yaml | $(K) apply -f - >/dev/null
	$(K) delete job fern-bench-db fern-bench-load --ignore-not-found
	sed "s#image: IMAGE#image: $(BENCH_IMG)#" infra/k8s/job-bench-db.yaml | $(K) apply -f -
	$(K) wait --for=condition=complete job/fern-bench-db --timeout=300s
	$(K) create configmap fern-bench-script --from-file=bench/load.py --dry-run=client -o yaml | $(K) apply -f -
	# The load generator first (it is the biggest pod and waits for the API), then the API copies.
	sed -e "s#image: IMAGE#image: $(BENCH_IMG)#" \
	  -e 's#args: \[\]#args: ["--stages", "$(BENCH_STAGES)", "--procs", "$(BENCH_PROCS)", "--replicas", "$(BENCH_REPLICAS)", "--database", "$(BENCH_DB_DESC)"]#' \
	  -e 's#cpu: LOAD_CPU#cpu: $(BENCH_LOAD_CPU)#' infra/k8s/job-bench-load.yaml | $(K) apply -f -
	sed -e "s#image: IMAGE#image: $(BENCH_IMG)#" -e 's#replicas: REPLICAS#replicas: $(BENCH_REPLICAS)#' \
	  -e 's#cpu: API_CPU#cpu: $(BENCH_API_CPU)#' infra/k8s/bench-api.yaml | $(K) apply -f -
	$(K) rollout status deployment/fern-bench --timeout=300s
	@until [ -n "$$($(K) get job fern-bench-load -o jsonpath='{.status.succeeded}{.status.failed}')" ]; do sleep 10; done; \
	if [ "$$($(K) get job fern-bench-load -o jsonpath='{.status.succeeded}')" != 1 ]; then \
	  $(K) logs job/fern-bench-load --tail=30; echo "bench job failed"; exit 1; fi
	@mkdir -p bench/results
	$(K) logs job/fern-bench-load | sed -n '/^BENCH_JSON_BEGIN$$/,/^BENCH_JSON_END$$/p' | sed '1d;$$d' \
	  | python3 -m json.tool > bench/results/latest.json
	@cp bench/results/latest.json bench/results/$$(date -u +%Y%m%dT%H%MZ).json
	$(K) delete deployment/fern-bench service/fern-bench job/fern-bench-load job/fern-bench-db --ignore-not-found
	@python3 -c "import json; r=json.load(open('bench/results/latest.json')); print(r['headline']); print(r['money'])"

# The static archive the Worker serves from LIVE_UNTIL on (infra/cloudflare/wrangler.toml):
# the frontend, the load-test result and the recorded live run (src/fern/web/archive.json,
# recorded with ?record=1, see docs/DEPLOY.md).
ARCHIVE_DIR := infra/cloudflare/archive
archive:
	mkdir -p $(ARCHIVE_DIR)
	cp src/fern/web/index.html src/fern/web/bench.json $(ARCHIVE_DIR)/
	@if [ -f src/fern/web/archive.json ]; then cp src/fern/web/archive.json $(ARCHIVE_DIR)/; \
	else echo "warning: src/fern/web/archive.json missing; the archive would have no recorded run"; fi

worker: archive  ## deploy the Worker (routing now, the archive after LIVE_UNTIL)
	cd infra/cloudflare && npx wrangler@4 deploy

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
