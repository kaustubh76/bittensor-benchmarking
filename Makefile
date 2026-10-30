.PHONY: deploy-mainnet-dry deploy-mainnet verify-mainnet prove-human verify-loop help setup test test-py golden golden-check anchors-fetch anchors-report anchors-check evalset evalset-check rate rate-bless test-contracts test-agent test-terminal pipeline demo demo-agent demo-full eval eval-gate openapi-doc openapi-doc-check ci snapshot api terminal agent agent-live interop build-contracts anvil onchain deploy-testnet-dry deploy-testnet deploy-mirror-dry deploy-mirror deploy-humanid-dry deploy-humanid deploy-oracle-v2-dry deploy-oracle-v2 backfill-oracle-v2 verify-testnet post-once attest-once seed-sellers mirror-receipts resolve-humans recompute futures-roll futures-settle futures-withdraw futures-collateralize aomi-live verify-live verify-live-testnet verify-claims x402-capture wallet-settle-probe desk-preflight desk-e2e desk-evidence tape-audit lint glossary-check diagram diagram-preview clean graph-abis graph-install graph-codegen graph-build graph-test graph-deploy circle-check circle-login buyer-key circle-wallet circle-fund circle-deposit circle-balance gateway-deposit gateway-balance skills-install verify-operator sandbox-decisions sandbox-decisions-check archive-decisions archive-decisions-check ledger operator-run subnet-demo subnet-demo-real subnet-snapshot subnet-tape subnet-tape-seed subnet-canary subnet-gate subnet-oracle subnet-join subnet-serve subnet-publish subnet-identity subnet-image

help:
	@echo "ACR — The Arc Compute Rate"
	@echo ""
	@echo "  THE PRODUCT — a spend operator that pays a business's machine bills:"
	@echo "  make operator-run    BUSINESS=slug [LIVE=1] — price, meter, screen, decide, pay"
	@echo "  make ledger          BUSINESS=slug — those decisions as beancount that balances"
	@echo "  make verify-operator probe the deployed operator surfaces end to end"
	@echo "  make verify-drift    does the DEPLOYMENT serve what this checkout builds?"
	@echo ""
	@echo "  the benchmark it prices against, and the tools that build and prove it:"
	@echo "  make setup           install python + node + foundry deps"
	@echo "  make test            run everything (python + contracts + agent + terminal)"
	@echo "  make pipeline        run the estimator on simulated exhaust (live prints)"
	@echo "  make demo            run the 'Attack the Index' demo"
	@echo "  make eval            produce the ACR-vs-VWAP error chart data"
	@echo "  make eval-gate       assert the headline resistance claims (CI gate)"
	@echo "  make ci              lint + full test suite + eval gate (mirrors GitHub CI)"
	@echo "  make snapshot        regenerate the Terminal's bundled snapshot"
	@echo "  make openapi-doc     render docs/acr-openapi.md (+pdf) from app.openapi(); -check fails when stale"
	@echo "  make demo-agent      the agent module in ten acts: cards, tiers, the screen (ACR_ARMOR_* for Model Armor)"
	@echo "  make demo-full       every demo in order: run_demo, demo-agent, then the claim audit"
	@echo "  make prove-human     the World path, live: challenge -> signed proof -> one TCA per PERSON"
	@echo "  make verify-loop     the deployed /loop page: every instrument's route, asserted (read-only)"
	@echo "  make anvil           run a local anvil chain (:8545)"
	@echo "  make onchain         deploy + post prints on-chain + settle (needs anvil)"
	@echo ""
	@echo "  Arc testnet (docs/TESTNET_RUNBOOK.md is the ordered operator sequence):"
	@echo "  make deploy-testnet-dry  simulate the Foundry deploy against Arc testnet (no broadcast)"
	@echo "  make deploy-testnet  deploy ACROracle + AttestationRegistry to Arc testnet"
	@echo "  make verify-testnet  read-only checks: chain id, code, signer, latest prints"
	@echo "  make deploy-mainnet-dry  simulate all five deploys on Arc MAINNET (refuses a wrong chain id; docs/MAINNET_RUNBOOK.md)"
	@echo "  make verify-mainnet      the mainnet preflight with custody ENFORCED: owners, no pending transfer, deploy key retired"
	@echo "  make deploy-mainnet  broadcast them, in order"
	@echo "  make post-once       one estimator cycle → signed postPrint txs on Arc"
	@echo "  make attest-once     write the demo sellers' EIP-712 attestations on-chain"
	@echo "  make seed-sellers    tiny USDC transfers so ArcSource sees the attested sellers"
	@echo "  make api             serve the x402-gated index API (:8000)"
	@echo "  make terminal        run the ACR Terminal (:3000; ACR_API=<seller url>, ACR_BUYER_PRIVATE_KEY enables the LIVE buyer)"
	@echo "  make lint            ruff check the python packages"
	@echo ""
	@echo "  make futures-roll    open a fresh series before the current one expires (idempotent)"
	@echo "  make futures-settle  cash-settle expired series so collateral can be withdrawn"
	@echo "  make futures-collateralize  deepen the book so the desk can fill the size it offers (dry by default)"
	@echo ""
	@echo "  the Public Desk (readers trade ACRFutures with a Circle user-controlled wallet):"
	@echo "  make desk-preflight  read-only gates: series life, margin capacity, custody balance"
	@echo "  make desk-e2e        drive the real browser PIN ceremony end to end (PLAYWRIGHT_DIR=…)"
	@echo "  make desk-evidence   confirm that run on-chain (USER_ID=… adds Circle's fee ledger)"
	@echo "  make tape-audit      measure what REAL Arc settlement flow yields as an index"
	@echo "  make sandbox-decisions       regenerate the demo fixtures by running the ladder"
	@echo "  make archive-decisions       fold production's decisions into the committed archive"
	@echo "  make archive-decisions-check exit 1 if production holds rows the archive does not"
	@echo ""
	@echo "  buyer agent (apps/agent — the machine side of the marketplace):"
	@echo "  make agent           offline demo: discover the catalog, pay the dev gate"
	@echo "                       (run ACR_X402_MODE=dev make api in another shell)"
	@echo "  make agent-live      Arc testnet: real Gateway x402 settlement"
	@echo "                       (needs AGENT_PRIVATE_KEY + a Gateway deposit — see docs/agent-runbook.md)"
	@echo "  make interop         check our 402 parses the way GatewayClient does (needs make api)"
	@echo ""
	@echo "  live wallet ops (interactive Circle CLI — never run in CI):"
	@echo "  make circle-check    verify the Circle CLI is installed"
	@echo "  make circle-login    EMAIL=you@example.com — email-OTP login (testnet)"
	@echo "  make buyer-key       generate a testnet buyer EOA (raw key for GatewayClient)"
	@echo "  make circle-wallet   import the buyer key as a local wallet"
	@echo "  make circle-fund     ADDR=0x… — testnet faucet into the wallet"
	@echo "  make circle-deposit  ADDR=0x… — deposit USDC into Gateway (min 0.5)"
	@echo "  make circle-balance  ADDR=0x… — wallet + Gateway balances"
	@echo "  make gateway-deposit AMOUNT=0.5 — same deposit via SDK, no OTP session (needs AGENT_PRIVATE_KEY)"
	@echo "  make gateway-balance read the unified balance (needs AGENT_PRIVATE_KEY)"
	@echo "  make skills-install  install Circle Skills into .claude/skills"

	@echo ""
	@echo "  the Bittensor subnet (local — no chain, no netuid, no TAO):"
	@echo "  make subnet-demo     run four competing miners, scored and weighted (offline)"
	@echo "  make subnet-demo-real same, from the recorded subnet 64 tape (offline)"
	@echo "  make subnet-snapshot refresh the committed tape fixtures (network; manual)"
	@echo "  make subnet-tape     append today to the cumulative archive (network; manual)"
	@echo "  make subnet-canary   do the upstreams still match what we parse? (network)"
	@echo "  make subnet-gate     assert the subnet pays the right miner"
	@echo "  make subnet-oracle   post the consensus print to ACROracle on a local anvil"

setup:
	uv sync --all-packages
	cd contracts && forge install --no-git foundry-rs/forge-std || true
	cd apps/terminal && npm install --no-audit --no-fund
	cd apps/agent && npm install --no-audit --no-fund

test: test-py test-contracts test-agent test-terminal

test-py:
	uv run pytest packages services tests -q -p no:cacheprovider --import-mode=importlib

# The estimator's frozen output. `--check` fails when a published number moves;
# re-bless with `make golden` in the SAME commit as the change that moved it.
golden:
	uv run python scripts/gen_golden.py

golden-check:
	uv run python scripts/gen_golden.py --check

test-contracts:
	cd contracts && forge test

test-agent:
	cd apps/agent && npm run build && npm test

test-terminal:
	cd apps/terminal && npm test && npm run lint && npm run build

# All THREE indices. It ran ACR-INF only for months, so ACR-GPU and ACR-DATA
# were gated by nothing but the paired-swing tests — which measure a swing
# against a clean run, not error against truth. Both clear the same thresholds
# today with margin (GPU 63.2 bp / 87.5x, DATA 106.1 bp / 48.7x), so this costs
# nothing but the runtime and closes the hole.
eval-gate:
	uv run python scripts/eval.py --hours 12 --check --index ACR-INF
	uv run python scripts/eval.py --hours 12 --check --index ACR-GPU
	uv run python scripts/eval.py --hours 12 --check --index ACR-DATA

ci: lint test eval-gate golden-check anchors-check openapi-doc-check sandbox-decisions-check

build-contracts:
	cd contracts && forge build

anvil:
	anvil

onchain: build-contracts
	uv run python scripts/onchain_demo.py

# --- Arc testnet deploy (Foundry; docs/TESTNET_RUNBOOK.md is the full sequence) ---
# Gas on Arc IS USDC (native system contract 0x3600…0000) — fund the deployer at
# https://faucet.circle.com (Arc Testnet) before broadcasting.
# If EIP-1559 fee estimation fails on Arc, retry the forge script with --legacy.

ACR_ARC_RPC_URL ?= https://rpc.testnet.arc.io

deploy-testnet-dry:
	@test -n "$(DEPLOYER_PRIVATE_KEY)" || { echo "DEPLOYER_PRIVATE_KEY not set — export the funded deployer key first (docs/TESTNET_RUNBOOK.md step 2)"; exit 1; }
	@echo "cd contracts && forge script script/Deploy.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key ***"
	@cd contracts && forge script script/Deploy.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key $(DEPLOYER_PRIVATE_KEY)

deploy-testnet:
	@test -n "$(DEPLOYER_PRIVATE_KEY)" || { echo "DEPLOYER_PRIVATE_KEY not set — export the funded deployer key first (docs/TESTNET_RUNBOOK.md step 2)"; exit 1; }
	@echo "cd contracts && forge script script/Deploy.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key *** --broadcast"
	@cd contracts && forge script script/Deploy.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key $(DEPLOYER_PRIVATE_KEY) --broadcast
	@echo ""
	@echo "  contracts live — code shows at https://testnet.arcscan.app/address/<ACROracle> (+ <AttestationRegistry>)"
	@echo "  now set in .env (value on the SAME line as '=', NO inline comments):"
	@echo "    ACR_ORACLE_ADDRESS=0x<ACROracle address from the log above>"
	@echo "    ACR_REGISTRY_ADDRESS=0x<AttestationRegistry address from the log above>"
	@echo "  then: make verify-testnet"

# Deploy ONLY ACRFutures against the already-deployed oracle (never redeploys it).
# Requires ACR_ORACLE_ADDRESS (the live oracle) + DEPLOYER_PRIVATE_KEY (funded).
deploy-futures-dry:
	@test -n "$(DEPLOYER_PRIVATE_KEY)" || { echo "DEPLOYER_PRIVATE_KEY not set — export the funded deployer key first"; exit 1; }
	@test -n "$(ACR_ORACLE_ADDRESS)" || { echo "ACR_ORACLE_ADDRESS not set — export the live oracle address first"; exit 1; }
	@echo "cd contracts && forge script script/DeployFutures.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key ***"
	@cd contracts && forge script script/DeployFutures.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key $(DEPLOYER_PRIVATE_KEY)

deploy-futures:
	@test -n "$(DEPLOYER_PRIVATE_KEY)" || { echo "DEPLOYER_PRIVATE_KEY not set — export the funded deployer key first"; exit 1; }
	@test -n "$(ACR_ORACLE_ADDRESS)" || { echo "ACR_ORACLE_ADDRESS not set — export the live oracle address first"; exit 1; }
	@echo "cd contracts && forge script script/DeployFutures.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key *** --broadcast"
	@cd contracts && forge script script/DeployFutures.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key $(DEPLOYER_PRIVATE_KEY) --broadcast
	@echo ""
	@echo "  ACRFutures live — code shows at https://testnet.arcscan.app/address/<ACRFutures>"
	@echo "  now set ACR_FUTURES_ADDRESS=0x<address above> in .env + on the Render seller."

deploy-oracle-v2-dry:
	@test -n "$(DEPLOYER_PRIVATE_KEY)" || { echo "DEPLOYER_PRIVATE_KEY not set — export the funded deployer key first"; exit 1; }
	@echo "cd contracts && forge script script/DeployOracleV2.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key ***"
	@cd contracts && forge script script/DeployOracleV2.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key $(DEPLOYER_PRIVATE_KEY)

deploy-oracle-v2:
	@test -n "$(DEPLOYER_PRIVATE_KEY)" || { echo "DEPLOYER_PRIVATE_KEY not set — export the funded deployer key first"; exit 1; }
	@echo "cd contracts && forge script script/DeployOracleV2.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key *** --broadcast"
	@cd contracts && forge script script/DeployOracleV2.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key $(DEPLOYER_PRIVATE_KEY) --broadcast
	@echo ""
	@echo "  ACROracleV2 live. Set ACR_ORACLE_V2_ADDRESS — and LEAVE ACR_ORACLE_ADDRESS"
	@echo "  on v1: ACRFutures settles against it and cannot be repointed."
	@echo "  Then run 'make backfill-oracle-v2' BEFORE the poster takes a live v2 print."

# Re-sign v1's recent history under the v2 typehash. MUST run before the first
# live v2 post: postPrint enforces strictly monotone timestamps, so an earlier
# print can never be inserted afterwards.
backfill-oracle-v2:
	uv run python scripts/backfill_oracle_v2.py $(ARGS)

deploy-mirror-dry:
	@test -n "$(DEPLOYER_PRIVATE_KEY)" || { echo "DEPLOYER_PRIVATE_KEY not set — export the funded deployer key first"; exit 1; }
	@echo "cd contracts && forge script script/DeployReceiptMirror.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key ***"
	@cd contracts && forge script script/DeployReceiptMirror.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key $(DEPLOYER_PRIVATE_KEY)

deploy-mirror:
	@test -n "$(DEPLOYER_PRIVATE_KEY)" || { echo "DEPLOYER_PRIVATE_KEY not set — export the funded deployer key first"; exit 1; }
	@echo "cd contracts && forge script script/DeployReceiptMirror.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key *** --broadcast"
	@cd contracts && forge script script/DeployReceiptMirror.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key $(DEPLOYER_PRIVATE_KEY) --broadcast
	@echo ""
	@echo "  ReceiptMirror live — the subgraph's settlement tape."
	@echo "  Set ACR_RECEIPT_MIRROR_ADDRESS in .env + on the Render seller, then put"
	@echo "  the address AND this deploy's block number into graph/subgraph.yaml."

# Prove the deployed operator surfaces: the business list, one Spend Statement
# and its beancount ledger — and the honesty properties, not just the status
# codes. VERIFY_TERMINAL_URL retargets it at a local next dev.
verify-operator:
	uv run python scripts/verify_operator.py

# One business's decisions as a beancount file. Prior Art #01 names beancount as
# "a ledger an agent can write to" and says it has never been connected to money
# that actually moves; this is that connection, and every transaction in it sums
# to zero. BUSINESS is a registry slug or a treasury address.
ledger:
	@test -n "$(BUSINESS)" || { echo "BUSINESS not set — e.g. make ledger BUSINESS=acr-fleet"; exit 1; }
	uv run python -c "import sys; from index_api.businesses import resolve; from index_api.ledger_export import to_beancount; from index_api.statement import read_decisions; b=resolve('$(BUSINESS)'); sys.exit('no business registered as $(BUSINESS)') if b is None else sys.stdout.write(to_beancount(read_decisions(business=b.slug), b.slug))"

# Fold the operator's live decisions into the committed archive, for exactly the
# reason x402-capture exists: production has no persistent disk, so every
# decision made since the last deploy lives on a volume the next deploy erases.
# Without this the traction page quietly reverts to the rows that ship in the
# image — a smaller number, not a broken page, which is the failure shape nobody
# investigates. Run it BEFORE a redeploy, then commit the archive.
# The sandbox's decision fixtures, DERIVED rather than typed. One of the six was
# hand-written to describe a decision the ladder cannot reach (a 140 USDC bill
# escalating on the per-payment limit, when an unpriceable bill meets the
# unbenchmarked ceiling first), and it rendered in the escalation queue — which
# cannot be shown at all without a sandbox. `--check` is in `make ci`.
sandbox-decisions:
	uv run python scripts/gen_sandbox_decisions.py

sandbox-decisions-check:
	uv run python scripts/gen_sandbox_decisions.py --check

archive-decisions:
	uv run python scripts/archive_decisions.py

# The preflight form: exits 1 when production holds decisions the archive does
# not, so a redeploy can refuse to throw them away.
archive-decisions-check:
	uv run python scripts/archive_decisions.py --check

# Run the spend operator for one business. Dry run unless LIVE=1, and --live is
# refused for a business with no PolicyWallet rather than doing a dry run under
# the wrong name.
operator-run:
	@test -n "$(BUSINESS)" || { echo "BUSINESS not set — e.g. make operator-run BUSINESS=acr-fleet"; exit 1; }
	uv run python scripts/operator_run.py --business $(BUSINESS) $(if $(LIVE),--live,)

# One business's spending authority. ONE WALLET PER BUSINESS: a shared wallet with
# per-business categories would put one business's budget one wrong `isAgent` entry
# away from another's agent. A deploy costs about a cent on Arc.
# The deploy authorizes no agent and sets no budget, so a fresh wallet can hold
# money and spend none of it — that is the safe resting state. ACR_POLICY_AGENT
# authorizes the operator at deploy time; a budget is always a separate call.
deploy-policy-dry:
	@test -n "$(DEPLOYER_PRIVATE_KEY)" || { echo "DEPLOYER_PRIVATE_KEY not set — export the funded deployer key first"; exit 1; }
	@echo "cd contracts && forge script script/DeployPolicyWallet.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key ***"
	@cd contracts && forge script script/DeployPolicyWallet.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key $(DEPLOYER_PRIVATE_KEY)

deploy-policy:
	@test -n "$(DEPLOYER_PRIVATE_KEY)" || { echo "DEPLOYER_PRIVATE_KEY not set — export the funded deployer key first"; exit 1; }
	@echo "cd contracts && forge script script/DeployPolicyWallet.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key *** --broadcast"
	@cd contracts && forge script script/DeployPolicyWallet.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key $(DEPLOYER_PRIVATE_KEY) --broadcast
	@echo ""
	@echo "  PolicyWallet live — it owns nothing until you fund it, and permits"
	@echo "  nothing until setAgent + setBudget run. Set ACR_POLICY_WALLET_ADDRESS."

# The human-grouping mirror. Records WINDOW-ROTATED CLUSTER IDS, never a World ID
# nullifier — see contracts/src/HumanIdMirror.sol for why rotation prevents
# cross-service correlation but not within-tape fleet linkage.
# ACR_HUMANID_SALT_COMMITMENT is keccak256(salt) and is REQUIRED. Never pass the
# salt itself: constructor args land in contracts/broadcast/, which is committed.
deploy-humanid-dry:
	@test -n "$(DEPLOYER_PRIVATE_KEY)" || { echo "DEPLOYER_PRIVATE_KEY not set — export the funded deployer key first"; exit 1; }
	@test -n "$(ACR_HUMANID_SALT_COMMITMENT)" || { echo "ACR_HUMANID_SALT_COMMITMENT not set — export keccak256(salt), NOT the salt"; exit 1; }
	@echo "cd contracts && forge script script/DeployHumanIdMirror.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key ***"
	@cd contracts && forge script script/DeployHumanIdMirror.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key $(DEPLOYER_PRIVATE_KEY)

deploy-humanid:
	@test -n "$(DEPLOYER_PRIVATE_KEY)" || { echo "DEPLOYER_PRIVATE_KEY not set — export the funded deployer key first"; exit 1; }
	@test -n "$(ACR_HUMANID_SALT_COMMITMENT)" || { echo "ACR_HUMANID_SALT_COMMITMENT not set — export keccak256(salt), NOT the salt"; exit 1; }
	@echo "cd contracts && forge script script/DeployHumanIdMirror.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key *** --broadcast"
	@cd contracts && forge script script/DeployHumanIdMirror.s.sol --rpc-url $(ACR_ARC_RPC_URL) --private-key $(DEPLOYER_PRIVATE_KEY) --broadcast
	@echo ""
	@echo "  HumanIdMirror live — the tape's record of who is one human."
	@echo "  Set ACR_HUMANID_MIRROR_ADDRESS in .env + on the Render seller, then put"
	@echo "  the address AND this deploy's block number into graph/subgraph.yaml"
	@echo "  and redeploy the subgraph with a NEW VERSION (it re-indexes)."
	@echo "  Then 'make resolve-humans' BEFORE generating any further tape:"
	@echo "  Settlement.human is stamped at finalize and cannot be revised."

verify-testnet:
	uv run python scripts/verify_deploy.py

# The mainnet preflight: verify_deploy against the mainnet RPC with the custody checks
# ENFORCED. Off mainnet those checks print ⚠ and pass; on chain 5042 they are ✗ and
# fail — a deploy key that still owns or signs anything stops the launch here.
verify-mainnet:
	@test -n "$(ACR_MAINNET_RPC_URL)" || { echo "ACR_MAINNET_RPC_URL not set"; exit 1; }
	@test -n "$(ACR_EXPECTED_OWNER)" || { echo "ACR_EXPECTED_OWNER not set: the custody checks need to know who should own the contracts (docs/MAINNET_RUNBOOK.md §3 step 5)"; exit 1; }
	ACR_ARC_RPC_URL=$(ACR_MAINNET_RPC_URL) ACR_ARC_CHAIN_ID=$(ACR_MAINNET_CHAIN_ID) ACR_CUSTODY_STRICT=1 uv run python scripts/verify_deploy.py

# --- Arc MAINNET (eip155:5042; public genesis 2026-09-16) --------------------
# The same five Foundry scripts the testnet runs on, in the order their own
# console output demands (v1 bundle -> v2 -> mirrors). Nothing is defaulted here
# that differs between the chains: the RPC, the chain id and the USDC address
# must be stated on the command line, and the dry run refuses to proceed if the
# RPC answers with a different chain id than the one named. The full ordered
# sequence, with what to set after each step, is docs/MAINNET_RUNBOOK.md.
#
#   ACR_MAINNET_RPC_URL=https://rpc.arc.network ACR_MAINNET_USDC=0x... make deploy-mainnet-dry
#
ACR_MAINNET_CHAIN_ID ?= 5042
# One script, one broadcast: the venue on v2, the deploy key retired as a signer on every
# contract, ownership hand-over started (docs/SECURITY-AUDIT.md C1, C2).
MAINNET_SCRIPTS = DeployMainnet.s.sol

# How the mainnet deploy authenticates. Prefer a Foundry keystore:
#
#   cast wallet import acr-deploy --interactive     # once; prompts for key + password
#   ACR_DEPLOY_ACCOUNT=acr-deploy make deploy-mainnet
#
# The key then lives encrypted in ~/.foundry/keystores, never in .env (which any
# tool reading this repo can cat) and never in argv (which `ps aux` can read on a
# shared machine). DEPLOYER_PRIVATE_KEY still works, and is fine for anvil and
# testnet, where the key guards nothing of value.
ifdef ACR_DEPLOY_ACCOUNT
deploy_signer := --account $(ACR_DEPLOY_ACCOUNT)
else
deploy_signer := --private-key $(DEPLOYER_PRIVATE_KEY)
endif

define mainnet_preflight
	@test -n "$(ACR_MAINNET_RPC_URL)" || { echo "ACR_MAINNET_RPC_URL not set (docs/MAINNET_RUNBOOK.md step 1)"; exit 1; }
	@test -n "$(ACR_MAINNET_USDC)" || { echo "ACR_MAINNET_USDC not set: the USDC address is NOT defaulted on mainnet (docs/MAINNET_RUNBOOK.md step 1)"; exit 1; }
	@test -n "$(ACR_DEPLOY_ACCOUNT)$(DEPLOYER_PRIVATE_KEY)" || { echo "no deploy signer: set ACR_DEPLOY_ACCOUNT=<keystore name> (cast wallet import <name> --interactive) or DEPLOYER_PRIVATE_KEY"; exit 1; }
	@test -n "$(ACR_HUMANID_SALT_COMMITMENT)" || { echo "ACR_HUMANID_SALT_COMMITMENT not set: HumanIdMirror is deployed WITH its commitment (docs/MAINNET_RUNBOOK.md step 1)"; exit 1; }
	@test -n "$(ACR_PRESS_SIGNER)" || { echo "ACR_PRESS_SIGNER not set: the press custody wallet that will sign prints; the deploy key is retired as a signer in the same broadcast (docs/MAINNET_RUNBOOK.md)"; exit 1; }
	@got=$$(cast chain-id --rpc-url $(ACR_MAINNET_RPC_URL)); test "$$got" = "$(ACR_MAINNET_CHAIN_ID)" || { echo "RPC answers chain id $$got, expected $(ACR_MAINNET_CHAIN_ID) — wrong network, refusing"; exit 1; }
	@echo "  chain id $(ACR_MAINNET_CHAIN_ID) confirmed at $(ACR_MAINNET_RPC_URL)"
endef

deploy-mainnet-dry:
	$(mainnet_preflight)
	@for s in $(MAINNET_SCRIPTS); do \
	  echo ""; echo "▸ simulate $$s"; \
	  (cd contracts && ACR_USDC_ADDRESS=$(ACR_MAINNET_USDC) forge script script/$$s --rpc-url $(ACR_MAINNET_RPC_URL) $(deploy_signer)) || exit 1; \
	done
	@echo ""; echo "  dry run complete: every script simulates on chain $(ACR_MAINNET_CHAIN_ID). Nothing was broadcast."

deploy-mainnet:
	$(mainnet_preflight)
	@echo ""; echo "  BROADCASTING to chain $(ACR_MAINNET_CHAIN_ID). Each script prints the addresses to set before the next."
	@for s in $(MAINNET_SCRIPTS); do \
	  echo ""; echo "▸ broadcast $$s"; \
	  (cd contracts && ACR_USDC_ADDRESS=$(ACR_MAINNET_USDC) forge script script/$$s --rpc-url $(ACR_MAINNET_RPC_URL) $(deploy_signer) --broadcast) || exit 1; \
	done
	@echo ""; echo "  now follow docs/MAINNET_RUNBOOK.md from step 3 (addresses -> render.yaml mainnet profile -> subgraph -> resolve-humans)."

# Prove ACR answers inside Aomi's PRODUCTION chat: two Direct-mode turns to the
# App, failing unless acr_check_bill and acr_rate actually ran and their numbers
# equal the press's in the same minute. The Aomi counterpart of `cd mcp && npm
# run smoke`. Read-only (a guest chat session; nothing is paid or signed).
# AOMI_APP_ID / AOMI_CHAT_URL / ACR_API point it elsewhere.
aomi-live:
	cd integrations/aomi/live && npm ci --no-audit --no-fund && node check.mjs

# Prove the DEPLOYED product is live — every pillar, one exit code. Read-only
# and safe against production: no writes, no faucet drips, no Circle users.
# VERIFY_STRICT=1 also fails on cron age and funding runway (warnings by default,
# because those depend on GitHub's scheduler rather than on our code).
verify-live:
	uv run python scripts/verify_live.py

# The same audit against Arc TESTNET: its press, RPC and contracts, the terminal's
# testnet routes (`?chain=testnet`, added by verify_live itself), and the wallet
# that actually signs testnet prints. That is 0x8366…, a Circle-held signer on
# both testnet oracles — NOT the mainnet poster keepalive watches. Measured
# 2026-10-14: it held 0.0079 USDC and testnet prints were 29 days stale.
verify-live-testnet:
	ACR_API_URL=https://acr-api-1fto.onrender.com \
	ACR_ARC_CHAIN_ID=5042002 \
	ACR_ARC_RPC_URL=https://rpc.testnet.arc.io \
	ACR_ORACLE_ADDRESS=0x4f00e3BDd224F4c4b4958D54cD774E84B9092609 \
	ACR_ORACLE_V2_ADDRESS=0xFCa038CEad7b9e9aa8fDAfc9e80253835fB8FFEA \
	ACR_FUTURES_ADDRESS=0x29d97c629a8278f7ec4218ab0bd8baa9182642fe \
	ACR_REGISTRY_ADDRESS=0x23ae3E1A306824F0CBA0b6561cB7E5502f63dFb7 \
	ACR_CUSTODY_ADDRESS=0x8366968f84a343CF70941EBe858428643d825cb0 \
	uv run python scripts/verify_live.py

# Prove the deployed /loop PAGE — the routes its four instruments press, with the
# bodies they send, against the alias (VERIFY_TERMINAL_URL to point elsewhere).
# verify-live proves the seller; this proves what a judge clicks. Read-only.
verify-loop:
	uv run python scripts/verify_loop.py

# Re-measure every number the judge-facing docs claim and fail on drift. The
# claims are PARSED from the docs, not mirrored in the script, so a figure that
# moves is caught rather than quietly agreed with. CLAIMS_FAST=1 skips suite
# collection for a quick pass.
verify-claims:
	uv run python scripts/verify_claims.py

# Does the DEPLOYMENT serve what this checkout builds? Both directions: a route
# built and not served is an undeployed change (the product's own /operator/*
# pages were empty in production for exactly this reason), and a route served
# and not built is a stale image whose docs are about to go wrong. Read-only,
# one GET per host, no credentials.
verify-drift:
	uv run python scripts/verify_deploy_drift.py

# Fold the seller's real x402 settlements into the committed archive, so they
# survive the next restart (production has no persistent disk). Run it right
# after a live buy, then commit the archive — it lives under services/ because
# data/ is in BOTH .gitignore and .dockerignore, so a file there would reach
# neither the repo nor the image.
x402-capture:
	uv run python scripts/x402_capture.py

# Prove the HUMAN revenue path with a real payment: a wallet signs, Circle's
# facilitator settles, the seller serves the data, and the depositor's Gateway
# balance goes down on chain. Every other walletPayer test is a pure function,
# so this is the only thing that proves a visitor can actually buy. Spends real
# USDC and needs a funded Gateway balance, which is why it is not in `make ci`.
#
#   PROBE_KEY=0x... SELLER=https://your-seller make wallet-settle-probe
#
# The seller must report facilitator=circle; against the dev gate the probe
# refuses, because a mock header proves nothing.
wallet-settle-probe:
	@test -n "$(PROBE_KEY)" || { echo "PROBE_KEY not set: the payer's key (never logged). Needs a funded Gateway balance."; exit 1; }
	@cd apps/terminal && PROBE_KEY=$(PROBE_KEY) SELLER=$(or $(SELLER),http://127.0.0.1:8000) npm run --silent wallet-settle-probe

# The autonomous hedger: one Circle AGENT wallet buys the index over x402, then
# trades ACRFutures on what it just read. The only loop here that makes an
# economic decision — every other agent either pays or trades, never both.
# HEDGER_DRY_RUN=1 decides and logs without spending. Needs ACR_HEDGER_ADDRESS.
hedger:
	uv run python scripts/hedger.py --once

# Measure the gaps between on-chain prints — the empirical proof that the press
# is not sleeping, which app.SELF_URL explicitly defers to rather than asserts.
# The tail is the number that matters: ACRFutures will not settle against a
# print older than 120 minutes. Pass GAP_SINCE (or --since) as the moment a fix
# went live so earlier holes are not counted against it. A window containing
# redeploys proves nothing — a deploy wakes the box just as well.
print-gaps:
	uv run python scripts/print_gaps.py

# --- futures venue lifecycle (a series expires; the venue must outlive it) ---

# Idempotent: no-ops when a collateralized series still has life left, so it is
# safe on a timer. Exits non-zero rather than leaving an uncollateralized series.
futures-roll:
	uv run python scripts/futures_roll.py

# Permissionless. Refuses (rather than reverting) when the oracle print is too
# stale for the contract's freshness guard — the window reopens on the next print.
futures-settle:
	uv run python scripts/futures_settle.py

# Take a project key's collateral back out, across EVERY series it holds — a
# roll strands the old stake where nothing trades and nothing reclaims it.
# Reports only, unless you ask it to move money: WITHDRAW_DRY_RUN=0.
futures-withdraw:
	WITHDRAW_DRY_RUN=$${WITHDRAW_DRY_RUN-1} uv run python scripts/futures_withdraw.py

# The other direction: deepen the book so the desk can fill what it offers.
# "The maker is collateralized" and "the book can absorb a trade" are different
# facts, and the gap between them once cost this venue eleven frozen hours.
# Sizes itself in reader-sized trades against the desk's own feasible_qty, and
# refuses to spend the wallet below what funds the next roll. Reports only,
# unless you ask it to move money: COLLATERALIZE_DRY_RUN=0.
futures-collateralize:
	COLLATERALIZE_DRY_RUN=$${COLLATERALIZE_DRY_RUN-1} uv run python scripts/futures_collateralize.py

# --- the Public Desk (user-controlled wallets trading ACRFutures) ---

desk-preflight:
	uv run python scripts/desk_preflight.py

# Drives the REAL browser ceremony (a user-controlled key only exists client
# side). Playwright is not a repo dep — install it once, anywhere, and point
# PLAYWRIGHT_DIR at that node_modules. Needs `make api` + `make terminal` up.
desk-e2e:
	@test -n "$(PLAYWRIGHT_DIR)" || { echo "set PLAYWRIGHT_DIR=<dir>/node_modules (npm i playwright && npx playwright install chromium)"; exit 1; }
	node scripts/desk_e2e.mjs

tape-audit:
	uv run python scripts/tape_audit.py

desk-evidence:
	uv run python scripts/desk_evidence.py $(if $(USER_ID),--user $(USER_ID),)

post-once:
	uv run python scripts/post_once.py

recompute:
	uv run python scripts/recompute.py $(ARGS)

mirror-receipts:
	uv run python scripts/mirror_receipts.py $(ARGS)

resolve-humans:
	uv run python scripts/resolve_humans.py $(ARGS)

# A judge-runnable human proof: challenge -> CAIP-122 signature with a demo
# buyer's (public-by-construction) key -> one TCA across every wallet that human
# owns -> the nonce is spent. Against production by default; ARGS=--api ... for local.
prove-human:
	uv run python scripts/prove_human.py $(ARGS)

attest-once:
	uv run python scripts/attest_once.py

seed-sellers:
	uv run python scripts/seed_sellers.py

pipeline:
	uv run python scripts/run_pipeline.py --events 3000

demo:
	uv run python scripts/run_demo.py

# The agent card, the gate and the Model Armor screen, executed against a
# read-only API this target spawns itself. Real GCP: the injection in act 9 goes
# to the live template and comes back refused by name.
demo-agent:
	uv run python scripts/demo_agent.py

# The whole ladder, one exit code. SEPARATE RECIPE LINES, never `&&`: a chained
# target waits on a process that may never exit and silently skips everything
# after it, which is how a chained deck-and-pitch render once spent a week
# rendering nothing. Each leg prints its own verdict and make stops on the first failure.
demo-full:
	uv run python scripts/run_demo.py
	uv run python scripts/demo_agent.py
	uv run python scripts/verify_claims.py
	uv run python scripts/verify_live.py
	uv run python scripts/verify_loop.py

eval:
	uv run python scripts/eval.py --hours 12

snapshot:
	uv run python scripts/gen_snapshot.py

api:
	uv run uvicorn index_api.app:app --host 127.0.0.1 --port 8000 --reload

terminal:
	cd apps/terminal && npm run dev

# --- buyer agent (apps/agent) ---

agent:
	cd apps/agent && npm run start -- --dev --count 20 --discover --reroute

agent-live:
	cd apps/agent && npm run start -- --live --count 60 --limit 0.01 --discover --reroute

interop:
	cd apps/agent && npm run interop

# --- live wallet ops (interactive — Circle CLI; email OTP, so never in CI) ---
# Install: npm install -g @circle-fin/cli   (Node >= 20.18.2). Verified vs CLI v0.0.6.
# The buyer agent (apps/agent) signs EIP-3009 itself, so it needs a RAW exportable
# key. In v0.0.6 `circle wallet create` only makes custodied *agent* wallets, so a
# local wallet comes via `circle wallet import` (see `make buyer-key`). The Arc
# chain name is ARC-TESTNET; fund/deposit/balance all require --address + --chain.
# Re-check with `circle <verb> --help` / `circle blockchain list` if flags drift.

CIRCLE_CHAIN ?= ARC-TESTNET

circle-check:
	@command -v circle >/dev/null 2>&1 || { echo "Circle CLI not found — npm install -g @circle-fin/cli (Node >= 20.18.2)"; exit 1; }
	@circle --version

circle-login: circle-check
	@test -n "$(EMAIL)" || { echo "usage: make circle-login EMAIL=you@example.com"; exit 1; }
	circle wallet login $(EMAIL) --testnet

# Generate a fresh TESTNET-ONLY buyer EOA. Import it (next target) + export it as
# AGENT_PRIVATE_KEY for `make agent-live`. Never reuse a key that holds real funds.
buyer-key:
	@uv run python -c "from eth_account import Account; a=Account.create(); k=a.key.hex(); k=k if k.startswith('0x') else '0x'+k; print('  address:', a.address); print('  key:    ', k); print('  (testnet only — import with: circle wallet import buyer --private-key, and export AGENT_PRIVATE_KEY)')"

# Import the buyer key as a local (exportable) wallet — reads the key interactively.
# NAME must be unique in the vault; override if a name is already taken:
#   make circle-wallet NAME=buyer2
NAME ?= buyer
circle-wallet: circle-check
	circle wallet import $(NAME) --private-key

circle-fund: circle-check
	@test -n "$(ADDR)" || { echo "usage: make circle-fund ADDR=0x<buyer wallet>"; exit 1; }
	circle wallet fund --address $(ADDR) --chain $(CIRCLE_CHAIN)

# CLI v0.0.6: LOCAL wallets (circle wallet import) deposit on-chain and REJECT
# --method; AGENT wallets REQUIRE --method (eco|direct). So --method is only
# passed when METHOD= is set: local → `make circle-deposit ADDR=…`; agent →
# `make circle-deposit ADDR=… METHOD=direct`. USDC is Arc's native gas, so a
# funded wallet covers the deposit. Override the amount with AMOUNT=.
AMOUNT ?= 0.5
METHOD ?=
circle-deposit: circle-check
	@test -n "$(ADDR)" || { echo "usage: make circle-deposit ADDR=0x<buyer wallet>"; exit 1; }
	circle gateway deposit --amount $(AMOUNT) --address $(ADDR) --chain $(CIRCLE_CHAIN) $(if $(METHOD),--method $(METHOD))

circle-balance: circle-check
	@test -n "$(ADDR)" || { echo "usage: make circle-balance ADDR=0x<buyer wallet>"; exit 1; }
	circle wallet balance --address $(ADDR) --chain $(CIRCLE_CHAIN)
	circle gateway balance --address $(ADDR) --chain $(CIRCLE_CHAIN) --all

# The SDK path for the same deposit — Unified Balance Kit over the buyer's
# AGENT_PRIVATE_KEY (no CLI session, no kit key). `make circle-deposit` stays
# as the CLI/operator route; this one is the programmatic route an agent runs.
gateway-deposit:
	@test -n "$$AGENT_PRIVATE_KEY" || { echo "gateway-deposit needs AGENT_PRIVATE_KEY exported (see docs/agent-runbook.md §2b)"; exit 1; }
	cd apps/agent && npm run deposit -- --amount $(AMOUNT)

gateway-balance:
	@test -n "$$AGENT_PRIVATE_KEY" || { echo "gateway-balance needs AGENT_PRIVATE_KEY exported (see docs/agent-runbook.md §2b)"; exit 1; }
	cd apps/agent && npm run deposit -- --balances

skills-install: circle-check
	circle skill install --tool claude-code

# --- the tape subgraph (graph/) -----------------------------------------------
# The Graph indexes the live Arc contracts; `slippageBp` is computed in the
# mappings, not handed to them. ABIs are GENERATED from contracts/out so a
# contract change breaks codegen instead of breaking a query.

graph-abis: build-contracts
	uv run python scripts/graph_abis.py

graph-install:
	cd graph && npm ci --no-audit --no-fund

graph-codegen: graph-abis
	cd graph && npx graph codegen

#: Which Arc the subgraph indexes. `graph/networks.json` carries the address and
#: startBlock for each, and `graph build --network` REWRITES graph/subgraph.yaml
#: in place from it — so the committed manifest always reflects the last build,
#: and `NETWORK=arc-testnet make graph-build` is how you switch back. Defaults to
#: mainnet because that is what the product runs on since 2026-09-27.
NETWORK ?= arc

# `graph build` is itself the schema gate: it rejects a malformed @aggregation,
# an `arg` naming a field that does not exist, or a non-numeric aggregated field.
graph-build: graph-codegen
	cd graph && npx graph build --network $(NETWORK)

graph-test:
	cd graph && npm test   # pinned matchstick binary; see graph/package.json

# Studio deploy is an operator step: it needs network access and a Studio key.
#   cd graph && npx graph auth <deploy-key>
# The zero-address guard is not pedantry: a subgraph pointed at 0x0 indexes
# nothing, reports no error, and serves an empty tape that reads exactly like a
# quiet market. Fail here instead.
#: Version label for the Studio deployment; override per release.
VERSION ?= v0.1.0
#: The Studio SLUG to deploy to. It must already exist — the CLI cannot create
#: one, and Studio answers an unknown slug with a bare "Subgraph not found"
#: that looks exactly like a bad deploy key. Create it at thegraph.com/studio,
#: then `make graph-deploy SUBGRAPH=<slug>`.
#: `acr` since 2026-10-11 (mainnet, account 1763111). The older `ethonline`
#: slug on account 1758707 holds the TESTNET deployment — see GRAPH-RUNBOOK §5.
SUBGRAPH ?= acr

graph-deploy: graph-build
	@grep -q '"0x0000000000000000000000000000000000000000"' graph/subgraph.yaml \
	  && { echo "graph/subgraph.yaml still holds a placeholder ADDRESS."; \
	       echo "Deploy the contracts first (make deploy-mirror, make deploy-oracle-v2),"; \
	       echo "then fill in each address AND its deploy block."; exit 1; } || true
	@grep -q 'startBlock: 0$$' graph/subgraph.yaml \
	  && { echo "graph/subgraph.yaml still holds 'startBlock: 0'."; \
	       echo "That is not a small mistake: block 0 makes the indexer scan the whole"; \
	       echo "chain (60M+ blocks) instead of starting at the deploy. Fill in the real"; \
	       echo "block for every data source."; exit 1; } || true
	# `-l` is not optional in practice: without a version label the CLI opens an
	# interactive prompt, and the runbook's own step then hangs forever in CI or
	# under any non-tty caller. Override with `make graph-deploy VERSION=v0.2.0`.
	# No `--network` HERE, deliberately, even though graph/networks.json now exists
	# and `graph-build` takes one: passing it again at deploy time would rewrite
	# every address AFTER the two guards above have checked them, so the thing
	# deployed would not be the thing verified. `graph-build` has already written
	# the manifest for NETWORK; this step only ships it.
	cd graph && npx graph deploy $(SUBGRAPH) -l $(VERSION)

lint: glossary-check
	uv run ruff check packages services scripts redteam

glossary-check:
	uv run python scripts/check_glossary_coverage.py

diagram:
	uv run python scripts/gen_architecture.py
	# The one rendered view the repo keeps: the full canvas, embedded by
	# docs/ARCHITECTURE-DIAGRAM.md and held to the canvas by verify_claims.
	# Regenerated with the canvas, so the two cannot disagree.
	uv run python scripts/preview_excalidraw.py acr_architecture.excalidraw --out docs/assets --scale 0.5
	rm -f docs/assets/acr_architecture.preview.png

# docs/acr-openapi.md claimed for weeks to be "rendered from the live OpenAPI 3.1
# schema" with no recipe anywhere, and sat at 24 of 43 routes. This is the recipe.
# The check is in `make ci` so the next route added to app.py and nowhere else
# turns the reference red instead of silently stale. --no-stdin on marp, or the
# PDF step hangs forever whenever stdin is not a TTY.
openapi-doc:
	uv run python scripts/gen_openapi_doc.py
	npx -y @marp-team/marp-cli --no-stdin docs/acr-openapi.md -o docs/acr-openapi.pdf || echo "PDF export needs Chrome/Edge — the .md is ready"

openapi-doc-check:
	uv run python scripts/gen_openapi_doc.py --check

diagram-preview:
	uv run python scripts/preview_excalidraw.py

clean:
	rm -rf .venv contracts/out contracts/cache apps/terminal/.next apps/agent/node_modules scripts/_out
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

# ── anchors ──────────────────────────────────────────────────────────────────
# Real public prices for the three indices, dated and cited. `--fetch` is manual
# and hits the network; it writes anchors/ and NEVER touches indices.py, because
# re-anchoring moves the tape's price pin and breaks comparability with the
# prints already on chain. `--check` is offline and belongs in CI.
anchors-fetch:
	uv run python scripts/anchors.py --fetch

anchors-report:
	uv run python scripts/anchors.py --report

anchors-check:
	uv run python scripts/anchors.py --check

# Walk the DEMO's own path against the live hosts and say what a viewer sees.
# `test` says the code is right and `verify-drift` says the deployment serves
# what we built; neither answers "does the thing we are about to show in front
# of people work right now". Reads only: no transaction, no signature, no spend.
# Strict by default, because at rehearsal time "I could not read the host" is
# the most important failure — a sleeping free-tier press is exactly what
# happens on stage.
#   make rehearse
#   make rehearse REHEARSE_API=https://acr-api-mainnet.onrender.com   # the control
rehearse:
	uv run python scripts/rehearse_demo.py

# The same demo, driven in a real browser. `rehearse` above reads the /api/*
# envelopes; the demo shows PAGES, and every page here is client-rendered — so a
# hydration error or a className that no longer matches the CSS leaves the API
# green and the screen blank. Needs Chrome; SKIPS cleanly without it, and is out
# of `make ci` for the same reason smoke and anchors-live are.
#   make rehearse-pages
#   make rehearse-pages REHEARSE_CHAIN=mainnet   # the control: the empty state
rehearse-pages:
	uv run python scripts/rehearse_pages.py

# Has mcp/package.json's version already been published? npm refuses a duplicate
# and `npx -y acr-mcp` keeps serving the old build, so the repo can look shipped
# while nothing moved. `prepublishOnly` cannot see this: it builds and tests, and
# neither knows what the registry holds. Network, never CI, strict on an
# unreadable registry. Run it before `npm run release`.
mcp-version-check:
	uv run python scripts/mcp_version_check.py

# The same clock, read off the HOST instead of the working tree. `anchors-check`
# above reads the files in this checkout; the press reads the ones baked into its
# image, so they agree only until somebody re-anchors and nothing couples them.
# Network, and NOT in CI on purpose: the presses are free-tier and cold-start, and
# a gate that goes red because a service was asleep gets ignored. Run it after a
# deploy, beside `make verify-drift`.
#   make anchors-live                                    # the probe press
#   make anchors-live HOST=https://acr-api-1fto.onrender.com
HOST ?= https://acr-probe-free.onrender.com
anchors-live:
	uv run python scripts/anchors.py --check-live $(HOST)

# ── the auto-rater ───────────────────────────────────────────────────────────
# Frozen eval sets on five scenarios per index (three of them held-out seeds),
# and a per-component quality gate against a blessed baseline. Distinct from the
# other two: eval-gate asks whether the published claims still hold, golden-check
# whether the engine's output moved at all, and this whether quality regressed.
# Demonstrated non-overlap: turning the sybil cap off PASSES eval-gate and fails
# here with nine named regressions.
evalset:
	uv run python scripts/gen_evalset.py

evalset-check:
	uv run python scripts/gen_evalset.py --check

rate:
	uv run python scripts/rate.py

# NOTE is required and the script refuses an empty one: a baseline without a
# stated reason is a number whose provenance died with the shell that made it.
rate-bless:
	uv run python scripts/rate.py --bless "$(NOTE)"

# --- the Bittensor subnet (local; docs/BITTENSOR.md) --------------------------
#
# The mechanism is arithmetic over the ACR estimator, so none of this needs a
# chain, a netuid or TAO — which is the point. `subnet-demo` must pass offline
# from committed fixtures, because a demo that depends on a third-party API
# being up is a demo that fails in front of an audience.
#
# SEPARATE RECIPE LINES, never `&&` — see the note on demo-full.
subnet-demo:
	uv run python scripts/subnet_demo.py --epochs 6

subnet-demo-real:
	uv run python scripts/subnet_demo.py --real

# Network, manual, never CI — the same contract as `anchors-fetch`. Rewrites
# tests/fixtures/bt/, so the diff is the upstream market moving and belongs in
# a commit somebody read.
subnet-snapshot:
	uv run python scripts/bt_snapshot.py --capture-fixture

# Append today's observation to the CUMULATIVE archive, then commit it. This is
# the forward-polling path and the only way either subnet grows a real series:
# Chutes usage retains ~12 days upstream, so a gap longer than that is a
# permanent hole. The archive lives under services/ because data/ is in BOTH
# .gitignore and .dockerignore (same reason as `x402-capture`), and production
# has no persistent disk. Dedupes on a natural key, so re-running is a no-op.
subnet-tape:
	uv run python scripts/bt_snapshot.py

# Offline and idempotent: folds the committed fixtures into the archive so the
# series starts at the fixture's oldest day instead of today. Run once.
subnet-tape-seed:
	uv run python scripts/bt_snapshot.py --seed-from-fixture

# Does either upstream still look like the thing we parse? Network, and the one
# Bittensor target that is ALLOWED to fail: every SN51/SN64 gate in CI reads a
# committed file, so CI could not notice a field rename, an added auth
# requirement or an endpoint disappearing. Asserts the contract each CONSUMER
# depends on — shape, a row-count floor well under the measured population, and
# the keys read by name — plus the usage x stats join, which is the one contract
# no single endpoint expresses. Runs on a schedule in bittensor-refresh.yml with
# continue-on-error: an outage at a third party is not a reason to stop
# shipping, it is a reason for somebody to know.
#   make subnet-canary                    # all six endpoints
#   make subnet-canary ARGS=--skip-heavy  # without the 21 MB stats fetch
subnet-canary:
	uv run python scripts/bt_canary.py $(ARGS)

# The gate asks the only question a subnet has: does it pay the right miner?
# A step, never a CI job — verify_claims counts top-level jobs in ci.yml and
# asserts the number against the docs.
subnet-gate:
	uv run pytest packages/acr_subnet packages/acr_tape -q -p no:cacheprovider --import-mode=importlib

# Opt-in: the subnet's consensus print on a contract. A reference rate is a
# number something SETTLES against, so until a print reaches a chain that
# enforces the benchmark invariants, the mechanism is a well-tested opinion.
#
# Its own anvil on its own port, with an explicit --chain-id. Port 8545 on this
# machine was found occupied by a foreign `anvil --chain-id 133`, which is what
# makes scripts/futures_roll.py fail with "chain id 133 == 31337" — so inheriting
# whatever is on the default port is how that bug reaches a second target.
#
# Exits 0 when anvil or the artifacts are missing: this is opt-in, and a missing
# local toolchain is not a failure of the subnet.
NETUID ?= 602
SUBNET_ANVIL_PORT ?= 8547
subnet-oracle:
	@command -v anvil >/dev/null 2>&1 || { echo "  — anvil not installed, skipping (see docs/BITTENSOR.md)"; exit 0; }
	@test -f contracts/out/ACROracle.sol/ACROracle.json || { echo "  — contracts not built, run: make build-contracts"; exit 0; }
	@anvil --silent --chain-id 31337 --port $(SUBNET_ANVIL_PORT) & echo $$! > .anvil.subnet.pid
	@for i in $$(seq 1 30); do \
	  curl -sf -X POST -H 'Content-Type: application/json' \
	    --data '{"jsonrpc":"2.0","method":"eth_chainId","params":[],"id":1}' \
	    http://127.0.0.1:$(SUBNET_ANVIL_PORT) >/dev/null && break; \
	  sleep 1; \
	done
	-uv run python scripts/subnet_oracle.py --rpc http://127.0.0.1:$(SUBNET_ANVIL_PORT)
	@kill `cat .anvil.subnet.pid` 2>/dev/null || true

# What it costs to join netuid 602 RIGHT NOW. Reads the burn off the chain
# rather than printing a table, because the table was wrong by ~1800x: the
# quoted τ0.0005 is an ESTABLISHED subnet's min_burn, while a fresh subnet
# starts near max_burn and decays. Same registration: τ0.902987384, then
# τ0.539024711, then τ0.013920908, all on one day. Read-only, signs nothing.
subnet-join:
	uv run python scripts/subnet_join.py --netuid $(NETUID) $(ARGS)

# The deployed shape: the miner a validator queries plus the read-only /subnet
# view, in one app. No chain writes and no wallet -- `serve` never constructs
# Chain, so this runs with no SDK at all.
subnet-serve:
	uv run python -m acr_subnet.adapters.bt_service --host 127.0.0.1 --port 8091 $(ARGS)

# Both DRY by default. Each reads the chain back before it claims anything,
# because on a commit-reveal subnet a receipt is not the storage -- and once,
# here, a publish succeeded while the script that printed its result died.
subnet-publish:
	uv run python scripts/subnet_publish.py --netuid $(NETUID) $(ARGS)

subnet-identity:
	uv run python scripts/subnet_identity.py --netuid $(NETUID) $(ARGS)

# The deploy image: its own Dockerfile, its own extra, its own ignore file.
# NOT the root Dockerfile -- that one is the seller API and is what the live
# acr-probe-free runs. Asserts the tape at build time, so an image whose /print
# would 503 cannot be built.
subnet-image:
	docker build -f deploy/subnet/Dockerfile -t acr-subnet:dev .

	@rm -f .anvil.subnet.pid
