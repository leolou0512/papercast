"""The local episode pipeline (SPEC.md section 11). Owner: A8.

The CLI's worker calls `run_job(job_dir, api, progress)`; run.py's docstring is the contract
(job.json, the job dir, results, UsageLimit and PipelineError)."""
from .errors import PipelineError, UsageLimit  # noqa: F401
from .run import run_job  # noqa: F401
