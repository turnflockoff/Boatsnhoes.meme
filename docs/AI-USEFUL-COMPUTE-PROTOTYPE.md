# Boats N Hoes — RX 470 AI Useful-Compute Prototype

> Status: design proposal / pre-implementation. This is not a working miner, validated GPU stack, deployed token, audited contract, or live reward program.

## 1. Objective

Build a small prototype that uses the available AMD Radeon RX 470 GPUs to perform useful AI inference tasks. A coordinator assigns work to GPU workers, checks submitted results, records accepted work in an off-chain reward ledger, and may later settle finalized rewards using an Ethereum ERC-20 token.

The accurate description is **verified useful-compute rewards prototype**. This is not Ethereum proof-of-work, does not secure Ethereum consensus, and is not a trustless proof-of-useful-work blockchain.

## 2. Reported hardware

| Component | Reported configuration |
|---|---|
| OS | Ubuntu Linux; exact release not yet confirmed |
| CPU | AMD Ryzen 5 5600X |
| System RAM | 32 GB |
| GPUs | AMD Radeon RX 470 cards; 4 GB and 8 GB VRAM configurations reported |
| GPU topology | Each card starts as an independent worker; VRAM is not automatically pooled |

Exact PCI IDs, kernel driver, Mesa version, Vulkan runtime, and Ubuntu release still need to be collected. Hardware compatibility is unverified until tested.

## 3. Proposed architecture

    Ryzen 5 5600X / Ubuntu host — 32 GB RAM
                       |
                Job coordinator
          queue / worker registry / policy
                       |
             Signed, unique AI jobs
                  /          \
          RX 470 worker A   RX 470 worker B
             4 GB VRAM        8 GB VRAM
                  \          /
                  result receipts
                       |
               Verification engine
       hashes / task checks / duplicate checks / audits
                       |
              Off-chain reward ledger
                       |
          Later: Ethereum ERC-20 settlement

### Components

1. **Coordinator:** Creates jobs, assigns unique IDs, pins task/model versions, tracks worker status, and manages deadlines.
2. **Worker agent:** Receives a signed job, runs the permitted task, and returns its result and receipt.
3. **Inference runtime:** Initial candidate is llama.cpp with its Vulkan backend, or another small Vulkan-capable workload. Start with a task that fits available VRAM.
4. **Verification engine:** Checks job identity, model/input hashes, output schema, deadlines, duplicate submissions, and task-specific correctness. It schedules independent re-runs or random audits where appropriate.
5. **Reward ledger:** Records accepted work off-chain during the prototype. No token transfers or minting occur in the first milestone.
6. **Ethereum settlement (later):** An ERC-20 and separately reviewed distribution/claim contract may settle finalized rewards after the compute system is proven. Ethereum is only the proposed settlement layer.

## 4. AMD software compatibility: test first

The RX 470 is an older Polaris-generation GPU. Current official AMD ROCm Radeon support matrices do not list the RX 470 as a supported GPU. ROCm is therefore not the default or guaranteed path.

First inspect the actual Ubuntu installation:

    lsb_release -a
    uname -r
    lspci -nnk | grep -A3 -E 'VGA|Display'
    vulkaninfo --summary
    glxinfo -B
    ls -l /dev/dri/

If vulkaninfo or glxinfo is missing, install only the relevant inspection utilities after confirming the Ubuntu release. Do not replace kernel or graphics drivers blindly.

The initial candidate is the Linux amdgpu kernel driver plus Mesa RADV Vulkan, followed by a small Vulkan compute/inference smoke test. Mesa support for a GPU family does not guarantee this exact card, kernel, firmware, and distribution combination will work. Confirm that both GPUs enumerate separately and test each independently.

References:
- [AMD ROCm Linux support matrix](https://rocm.docs.amd.com/projects/install-on-linux/en/latest/reference/system-requirements.html)
- [Mesa RADV Vulkan driver](https://docs.mesa3d.org/drivers/radv.html)
- [llama.cpp build documentation](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md)

## 5. First workloads

Start with small, bounded inference tasks rather than large model training:
- image classification or another deterministic classification benchmark;
- small-model inference with a pinned model and tokenizer;
- structured extraction with a strict output schema.

The 4 GB card is more constrained. The 8 GB card may handle larger quantized models, subject to runtime overhead and actual free VRAM. Do not assume training is practical. Benchmark memory use, throughput, correctness, temperature, power draw, and stability per device.

## 6. Job and receipt design

A job definition should include:
- globally unique job ID;
- project and task type;
- pinned model identifier and cryptographic model hash;
- input hash and immutable input reference;
- task parameters and output schema;
- creation and expiry timestamps;
- verification policy and expected result format;
- coordinator signature.

A worker receipt can include:

    {
      "job_id": "unique-job-id",
      "worker_id": "registered-worker-id",
      "model_id": "pinned-model-name",
      "model_hash": "sha256:...",
      "input_hash": "sha256:...",
      "result_hash": "sha256:...",
      "started_at": "UTC timestamp",
      "completed_at": "UTC timestamp",
      "runtime_ms": 0,
      "verification_status": "pending",
      "reward_status": "not_eligible"
    }

This receipt is an accounting record, not cryptographic proof that the GPU executed the model. A worker can fabricate a result and its hashes unless the system independently verifies the output.

## 7. Verification and abuse resistance

There is no universal, inexpensive cryptographic proof that an arbitrary AI model ran correctly on a particular GPU. The prototype must be honest about this limitation.

Use layered checks:
- Recompute hashes and validate exact job/model/input versions.
- Enforce unique job IDs and idempotent acceptance; reject duplicate submissions.
- Validate output schemas and deterministic answers where available.
- Independently repeat selected jobs on another worker or trusted verifier.
- Use random spot audits and hidden test cases for benchmark tasks.
- Detect implausible runtimes, repeated outputs, and worker collusion patterns.
- Keep auditable records of rejected, pending, and accepted work.
- Never treat self-reported runtime or GPU identity as proof of execution.
- For nondeterministic inference, define tolerances and task-specific scoring before rewards are calculated.

Replication and audits raise the cost of cheating; they do not mathematically guarantee every computation occurred.

## 8. Reward policy and Ethereum boundary

Use an off-chain ledger only during the prototype. No real-money value, token payout, exchange listing, or mining profitability is promised.

Before token integration, publish the maximum reward allocation and emission schedule, accepted-work scoring formula, duplicate/dispute rules, operator privileges and multisig policy, project end date and reward cutoff, claim deadline, and treatment of unclaimed rewards. Publish contract source, tests, independent audit, and deployment addresses.

If the project keeps the previously discussed six-month early-stop rule, define a fixed project end date first and calculate the reward cutoff as six calendar months earlier in UTC. Ethereum has no announced end date; the schedule must be project-defined. Any post-launch cutoff should be immutable or governed by clearly disclosed, constrained rules.

## 9. Implementation milestones

### Milestone 0 — Hardware inventory
Collect Ubuntu release, kernel, PCI device IDs, bound drivers, Vulkan device list, and device permissions. Confirm each GPU independently.

### Milestone 1 — Single-GPU inference smoke test
Run a small Vulkan inference benchmark on one RX 470. Record model, quantization, VRAM use, throughput, temperature, and errors. Repeat on the second card.

### Milestone 2 — Worker agent
Implement worker registration, signed job retrieval, task execution, receipt submission, retries, and structured logs.

### Milestone 3 — Coordinator and verification
Implement a local job queue, job lifecycle, deterministic validation, duplicate rejection, audit sampling, and an SQLite ledger.

### Milestone 4 — Two-worker test
Run both cards independently, compare throughput and result quality, test worker restarts and malformed/duplicate submissions, and measure host CPU/RAM overhead.

### Milestone 5 — Reward simulation
Calculate hypothetical rewards from accepted work and export an auditable report. Keep balances simulated and off-chain.

### Milestone 6 — Optional Ethereum testnet settlement
Only after compute and verification work: implement a capped test ERC-20 and claim/distributor flow, write tests, obtain independent review, and use testnet first. Mainnet deployment is a separate decision.

## 10. Proposed initial software stack

| Layer | Initial candidate |
|---|---|
| Host | Ubuntu Linux |
| GPU driver | Existing amdgpu stack; verify before changes |
| Compute API | Vulkan via Mesa RADV, subject to device testing |
| Inference | llama.cpp Vulkan backend or small Vulkan benchmark |
| Coordinator API | Python + FastAPI |
| Queue | In-process or SQLite-backed queue initially |
| Worker | Python agent plus inference executable |
| Prototype ledger | SQLite |
| Later database | PostgreSQL, if justified |
| Token settlement | Ethereum ERC-20 + separately reviewed distributor/claim contract |

Avoid Redis, Kubernetes, multi-node orchestration, or token contracts before the single-host prototype demonstrates useful, verifiable work.

## 11. Current status and non-claims

- Design document only; no implementation is claimed.
- Reported hardware has not yet been validated by command output.
- No RX 470 Vulkan inference test has been completed.
- No worker agent, coordinator, verifier, reward ledger, or smart contract has been implemented by this document.
- No token has been deployed and no rewards are live.
- Official ROCm support for RX 470 is not claimed.
- No profitability, guaranteed payout, or investment return is claimed.

**Next action:** collect the hardware inspection output, then select the smallest viable Vulkan inference test for each card.
