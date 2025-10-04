Here is a draft of what we're moving towards.


# Home Network Command & Control System

## Overview
A FastAPI-based command and control system for managing multiple machines on a home network. The system consists of three components:
- **Server**: Central coordinator running permanently on `10.0.0.6:30814`
- **CLI**: Interactive command-line interface for managing jobs and workers (prompt: `!>`)
- **Client**: Lightweight agent running on worker machines

## Architecture

### Communication Flow
```
CLI -> Server -> Client(s)
```
- All communication over HTTP
- Clients poll server for work via `get_work` (exponential backoff to 10s max)
- Server maintains job queue (max 50k jobs) and worker registry
- Server persists worker groups to `workers.json`
- Server writes job results to `jobs.log`

### Components

#### Server (FastAPI)
- Runs permanently on designated machine
- Manages worker registration and health (15s timeout, hardcoded)
- Maintains job queue with dependency resolution (max 50k jobs)
- Distributes jobs: first eligible worker that polls gets the job
- Tracks active job status only (no in-memory history)
- Persists worker group configuration to `workers.json`
- Logs all job results to `jobs.log` (append-only)
- Configuration loaded from `server.json`
- Workers identified by IP address (reconnect-safe, static IPs required)

#### CLI
- Connects to `10.0.0.6:30814` (hardcoded, fallback from localhost)
- Interactive prompt: `!>`
- Command history persisted via readline history file
- All commands manage the C2 system
- Executes `pdsh` commands locally using groups/IPs
- Auto-installs pdsh if missing (`sudo apt install pdsh`)
- Checks queue capacity before submission
- Handles job submission failures gracefully
- **Fails immediately** on connection errors (no retries) - user must fix server and retry
- Can view job log file with scrollable interface

#### Client
- Minimal implementation
- Registers with server on startup (reads from `client.json`)
- Reports OS, architecture, and available disk space at registration
- Polls for work with exponential backoff: 1s → 2s → 4s → 8s → 10s (max, hardcoded)
- Can run multiple jobs concurrently
- Job execution workflow:
  1. Receive job with command, stdin, and input_files
  2. Create job workspace: `/tmp/C2/{job_id}/`
  3. Write input files to workspace
  4. Execute command via `sh -c` with stdin piped, cwd set to workspace
  5. Capture stdout to `/tmp/C2/{job_id}/stdout`
  6. Collect files matching output_file_patterns to `/tmp/C2/{job_id}/files/`
  7. Report results (stdout, stderr, output_files base64-encoded) to server
  8. Workspace persists until manual deletion or worker restart
- Configuration loaded from `client.json`

## Worker Groups & Job Constraints

### Worker Groups
- Workers can be assigned to one or more groups (e.g., "gpu", "high-mem", "storage")
- Groups are ad-hoc (no pre-registration required)
- Groups persisted to `workers.json` on disk
- Used for targeted job execution and pdsh operations

### Job Constraints
1. **Group affinity**: Job must run on workers in specific group(s)
2. **Dependencies**: Jobs that must complete before this job starts
   - If dependency fails, dependent job also fails
   - Circular dependencies detected using iterative topological sort and rejected at submission time
   - Non-existent dependencies rejected at submission time
3. **Affinity**: Jobs that must/should run on same machine as dependency
   - If same-machine affinity cannot be satisfied (dependencies ran on different workers), job fails immediately
4. **Tags**: Named resource locks (e.g., "gpu:0", "gpu:1", "disk", "network")
5. **Tag locking**: Each tag represents a named lock; a job can only run if all required tags are available on the worker

**Tag Semantics (Named Locks)**:
- Tags are arbitrary string identifiers representing resources
- Each tag can only be held by one job at a time per worker
- Jobs must acquire ALL their tags before starting
- Tags are released when job completes/fails/cancels
- Tags are automatically released if worker times out (15s no contact)
- Workers can declare available tags (e.g., ["gpu:0", "gpu:1", "disk", "network"])
- Common patterns:
  - Exclusive resources: "gpu:0", "gpu:1" (specific GPU)
  - Shared resource types: "disk", "network", "memory"
  - Custom locks: "dataset:imagenet", "port:8080"

### Job Distribution
- **Strategy**: First eligible worker to poll receives the job (atomic claim)
- **Eligibility**: 
  - Worker matches group requirements
  - Dependencies satisfied
  - Affinity rules met
  - All required tags (locks) are available on the worker
- **Queue limit**: Maximum 50,000 pending jobs
- **Backpressure**: Job submission fails if queue is full (atomic submission for batch/split)
- **No timeout**: Jobs can run indefinitely

**Tag Lock Acquisition**:
1. Worker polls for work via `get_work`
2. Server finds first pending job that matches worker's group and has available tags
3. Server atomically acquires all job tags (marks them as held by job_id)
4. Job assigned to worker
5. On completion/failure/cancel/worker-timeout, tags released automatically

## CLI Commands

### Worker Management
- `list` - List all registered workers with status
- `groups` - List all worker groups
- `assign <ip> <group>` - Add worker to group
- `unassign <ip> <group>` - Remove worker from group
- `info <ip>` - Detailed worker information including:
  - Worker metadata (hostname, IP, OS, architecture, disk space)
  - Groups and available tags
  - Current job details (full job objects, not just IDs)
  - Status and last seen timestamp
- `set-tags <ip> <tag1,tag2,...>` - Set available tags/locks for worker
- `get-tags <ip>` - Show available and in-use tags for worker
- `cleanup-workspaces <ip|@all>` - Trigger workspace cleanup on workers
- `detect-tags <ip>` - Run hardware detection and suggest tag configuration

### Job Management
- `jobs` - List all active jobs (pending/running only)
- `job <job_id>` - Detailed job information
- `cancel <job_id>` - Cancel pending/running job
- `queue-status` - Show queue size and capacity
- `clear-completed` - Remove completed jobs from memory
- `log [n]` - Show last n job results from log file (default: 50)
  - Shows in reverse chronological order (most recent first)
  - Uses `less` for scrollable viewing (arrow keys, page up/down, search with /)
  - Displays: job_id, command (truncated), status, exit_code, timestamps
- `get-files <job_id> [output_dir]` - Download output files from completed job
  - Fetches files from server and saves to local directory
  - Defaults to current directory if output_dir not specified

### PDSH Integration
- `pdsh <target> <command>` - Execute pdsh command locally
  - `<target>` can be:
    - Group name: `@gpu` (groups prefixed with @)
    - IP address: `10.0.0.15`
    - Special: `@all` for all workers
  - Examples:
    - `pdsh @gpu nvidia-smi`
    - `pdsh 10.0.0.15 uptime`
    - `pdsh @all df -h`
  - Auto-installs pdsh if not found:
    1. Attempts `sudo apt install pdsh`
    2. Checks if install succeeded
    3. Retries command if successful
    4. Fails gracefully if install unsuccessful
- `verify-ssh <target>` - Verify SSH connectivity to workers

### Job Submission
- `submit <command> [options]` - Submit job with constraints
  - Options:
    - `--group=<group>` - Target worker group
    - `--depends=<job_id1,job_id2>` - Comma-separated dependency list
    - `--same-machine` - Must run on same machine as dependencies
    - `--tags=<tag1,tag2>` - Comma-separated tags (named locks required)
    - `--stdin=<string>` - Data to pass to command's stdin
    - `--stdin-file=<path>` - Read stdin from local file
    - `--upload=<local_path:remote_name>` - Upload file(s) for job to use
    - `--download=<pattern>` - Download pattern for output files (e.g., "*.png")
    - `--dry-run` - Validate job without submitting
    - Examples:
      - `submit "python train.py" --tags=gpu:0 --stdin-file=config.yaml`
      - `submit "convert input.jpg output.png" --upload=photo.jpg:input.jpg --download=output.png`

### Batch Operations
- `batch <file>` - Submit multiple commands from file (one per line)
  - Atomic submission: fails completely if would exceed queue capacity
  - Each line parsed as JSON first, then as plain command:
    - **JSON format**: `{"command": "...", "group": "...", "tags": [...], ...}`
    - **Plain text**: Treated as command with all defaults
    - Missing JSON fields default to None
  - Invalid JSON that's also not a valid command fails the entire batch (atomic all-or-nothing)
  - Example batch file:
    ```
    {"command": "python train.py", "group": "gpu", "tags": ["training"]}
    echo "simple command"
    {"command": "python eval.py", "depends": ["job-abc"], "same_machine": true}
    ```
  - Supports `--dry-run` flag
- `split <command_template> <input_file> [--group=<group>]` - Split work across workers
  - Command template uses `{}` placeholder
  - Each line of input_file substituted into placeholder
  - Creates one job per line
  - Optional `--group` flag to target specific worker group
  - Atomic submission: fails completely if would exceed queue capacity
  - Supports `--dry-run` flag

## Design Decisions

### Job State Machine
```
PENDING -> ASSIGNED -> RUNNING -> COMPLETED (removed from memory, logged)
                    -> FAILED (removed from memory, logged)
        -> FAILED (if dependency fails or same-machine affinity unsatisfiable)
        -> CANCELLED (removed from memory, logged)
```

**Key Decision**: No persistent job history in memory. Completed/failed/cancelled jobs are logged to `jobs.log` and removed from memory to keep the server lightweight.

**Dependency Failure Propagation**: If a job's dependency fails, the dependent job is automatically marked as failed and will never run.

**Same-Machine Affinity Failure**: If a job requires same-machine affinity but dependencies ran on different workers, the job fails immediately at assignment time.

### Worker Health
- Workers considered alive if they've called `get_work` within last 15 seconds
- Workers marked as disconnected after 15s of no contact
- Dead workers remain in registry but marked offline
- Jobs assigned to dead workers return to queue automatically
- Tags held by dead workers' jobs are automatically released
- No active health pings from server (polling-based only)

### Worker Identification
- Workers identified by IP address (primary key)
- Static IP addresses required (DHCP reservations recommended)
- Worker reconnect scenario: Same IP = update existing worker registration
- No UUIDs used for worker identification

### Result Storage
- Active job output stored in memory (no size limits - user takes responsibility)
- Completed job results appended to `jobs.log`
- Worker saves outputs to `/tmp/C2/{job_id}/`:
  - `stdout` - Full stdout capture
  - `files/` - Output files matching patterns
- Workspaces persist on worker until manual deletion or worker restart
- Job results include:
  - Full stdout/stderr in memory
  - Base64-encoded output files (no size limits)
- Log format: JSON lines (one JSON object per line)
- No log rotation (manual management)

### Authentication
- No authentication (internal trusted network)
- Command execution via `sh -c` (shell injection accepted - trusted users only)
- File path validation not enforced (trusted users only)

### Persistence
- **Workers and groups**: `workers.json` on disk (updated on changes)
- **Jobs**: In-memory only (ephemeral)
- **Job results**: `jobs.log` append-only log file
- **Server state**: Rebuilt from `workers.json` on startup
  - In-progress jobs marked as failed on server restart

### Queue Backpressure
- Hard limit: 50,000 pending jobs
- API endpoint: `GET /api/queue/status` returns current size and capacity
- CLI checks capacity before batch submissions
- Batch/split operations are atomic: all-or-nothing submission

## API Endpoints

### Server API

**Worker Management**
- `POST /api/workers/register` - Register new worker
  - Body: `{hostname, ip, os, arch, disk_available_gb, groups?}`
  - Returns: `{ip}` (worker identifier)
  - If IP already exists in workers.json: updates existing worker (reconnect scenario)
  - Otherwise: creates new worker entry
- `POST /api/workers/get-work/{ip}` - Poll for work (replaces heartbeat)
  - Body: `{current_job_status?}` (optional status update)
  - Returns: `{job}` or `{wait_seconds}`
  - Updates last_seen timestamp
  - Attempts to assign eligible job (checks group, dependencies, tag availability)
  - Atomically acquires job's required tags if assigned
- `GET /api/workers/list` - List all workers
- `GET /api/workers/info/{ip}` - Get worker details
  - Returns full worker object including:
    - Metadata (hostname, IP, OS, arch, disk space)
    - Groups and available tags
    - Tag lock status
    - **Full job objects** for all current jobs (not just IDs)
    - Status and timestamps
- `POST /api/workers/groups/{ip}` - Assign groups
  - Body: `{groups: [string]}`
- `DELETE /api/workers/groups/{ip}/{group}` - Unassign group
- `POST /api/workers/tags/{ip}` - Set available tags
  - Body: `{tags: [string]}`
- `GET /api/workers/tags/{ip}` - Get tag status
  - Returns: `{available_tags: [string], tag_locks: {tag: job_id|null}}`
- `POST /api/workers/cleanup/{ip}` - Trigger workspace cleanup
  - Returns: `{status: "ok"}` or error
- `POST /api/workers/detect-tags/{ip}` - Run hardware detection
  - Returns: `{suggested_tags: [string]}`

**Job Management**
- `POST /api/jobs/submit` - Submit new job
  - Body: `{command, group?, dependencies?, same_machine?, tags?, stdin?, input_files?, output_file_patterns?}`
  - Returns: `{job_id}` or error if queue full
  - Validates that required tags exist on at least one worker in target group
  - Validates dependencies exist using iterative topological sort
  - Returns error if dependencies are invalid or circular
  - HTTP status codes for errors (appropriate 4xx/5xx codes)
- `POST /api/jobs/submit-wait` - Submit job and wait for completion
  - Body: `{command, group?, dependencies?, same_machine?, tags?, stdin?, input_files?, output_file_patterns?}`
  - Returns: `{job_id, status, exit_code, stdout, stderr, output_files}` when job completes
  - Keeps connection alive until job finishes (long-polling)
  - No explicit timeout (can run indefinitely)
- `POST /api/jobs/submit-batch` - Submit multiple jobs atomically
  - Body: `{jobs: [{command, ...}]}`
  - Returns: `{job_ids: [string]}` or error if would exceed capacity
  - All-or-nothing: either all jobs submitted or none
- `POST /api/jobs/submit-batch-wait` - Submit batch and wait for all to complete
  - Body: `{jobs: [{command, ...}]}`
  - Returns: `{results: [{job_id, status, exit_code, stdout, stderr, output_files}]}`
  - Keeps connection alive until all jobs finish
  - Returns results in order of completion
- `GET /api/jobs/queue-status` - Get queue size and capacity
  - Returns: `{pending: int, running: int, capacity: int, available: int}`
- `GET /api/jobs/list` - List active jobs
- `GET /api/jobs/info/{job_id}` - Get job details
- `GET /api/jobs/files/{job_id}` - Get job output files
  - Returns: `{files: {filename: base64_content}}`
- `POST /api/jobs/cancel/{job_id}` - Cancel job
- `PUT /api/jobs/status/{job_id}` - Update job status (worker callback)
  - Body: `{status, exit_code?, stdout?, stderr?, output_files?}`
  - Releases job's tags when status is completed/failed/cancelled
- `DELETE /api/jobs/completed` - Clear completed jobs from memory
- `GET /api/jobs/log?lines=50` - Get recent job results from log file
  - Query: `?lines=<n>` (default: 50)
  - Returns: `{entries: [{job_id, command, status, ...}]}`

**PDSH Helper**
- `GET /api/pdsh/workers/{target}` - Get worker list for pdsh
  - Path: `{target}` = group name, "all", or ip
  - Returns: `{workers: [{ip, hostname}], hostnames: "host1,host2,..."}`

**Metrics**
- `GET /api/metrics` - Get server metrics
  - Returns: `{uptime_seconds: int, total_jobs_processed: int, active_workers: int, queue_depth: int}`

## Data Models

### Worker
```python
{
    "ip": "10.0.0.15",              # Primary identifier
    "hostname": "string",
    "os": "string",                  # e.g., "Linux", "Darwin"
    "arch": "string",                # e.g., "x86_64", "arm64"
    "disk_available_gb": float,      # Available disk space in GB
    "groups": ["string"],
    "available_tags": ["string"],    # Tags/locks this worker can provide
    "tag_locks": {                   # Current lock state
        "gpu:0": "job-id|null",      # null = available, job-id = held
        "gpu:1": "job-id|null",
        "disk": "job-id|null"
    },
    "status": "active|idle|busy|disconnected",
    "last_seen": "timestamp",
    "current_jobs": [                # List of full job objects
        {
            "id": "job-123",
            "command": "python train.py",
            "status": "running",
            ...
        }
    ],
    "registered_at": "timestamp"
}
```

### Job
```python
{
    "id": "job-{uuid}",
    "command": "string",
    "status": "pending|assigned|running|completed|failed|cancelled",
    "group": "string|null",
    "dependencies": ["job_id"],
    "same_machine": bool,
    "tags": ["string"],              # Required named locks
    "stdin": "string|null",          # Data to pipe to command's stdin
    "input_files": {                 # Files uploaded for this job
        "remote_name": "base64_content"
    },
    "output_file_patterns": ["string"],  # Glob patterns for files to capture
    "assigned_worker": "ip|null",
    "created_at": "timestamp",
    "started_at": "timestamp|null",
    "completed_at": "timestamp|null",
    "exit_code": "int|null",
    "stdout": "string",
    "stderr": "string",
    "output_files": {                # Captured output files
        "filename": "base64_content"
    }
}
```

### Job Log Entry (jobs.log format)
```json
{
  "job_id": "job-{uuid}",
  "command": "string",
  "status": "completed|failed|cancelled",
  "worker_ip": "string",
  "worker_hostname": "string",
  "group": "string|null",
  "tags": ["string"],
  "created_at": "timestamp",
  "started_at": "timestamp",
  "completed_at": "timestamp",
  "exit_code": "int|null",
  "stdout": "string",
  "stderr": "string",
  "output_files": ["filename1", "filename2"],
  "workspace": "/tmp/C2/{job_id}"
}
```

### Persistence Schema (workers.json)
```json
{
  "workers": {
    "10.0.0.15": {
      "hostname": "string",
      "ip": "10.0.0.15",
      "os": "string",
      "arch": "string",
      "disk_available_gb": float,
      "groups": ["string"],
      "available_tags": ["string"],
      "registered_at": "timestamp"
    }
  },
  "last_updated": "timestamp"
}
```

## Implementation Phases

**Phase 1**: Core infrastructure
- Server with worker registration and persistence (IP-based identification)
- `get_work` endpoint with 15s timeout tracking
- Client with polling and command execution
- Basic CLI with worker management
- Readline history file

**Phase 2**: Job queue
- Job submission with queue limit
- Job distribution (first-eligible-worker with atomic claim)
- Status tracking and updates
- Job logging to `jobs.log`
- Iterative topological sort for dependency validation

**Phase 3**: Constraints and PDSH
- Job dependencies and affinity
- Tag-based locking with automatic cleanup on worker timeout
- PDSH integration with auto-install
- Log viewing with `less` in CLI
- SSH verification helper

**Phase 4**: Batch operations and utilities
- Atomic batch file submission
- Split command for work distribution
- Error handling and recovery
- Dry-run mode
- Workspace cleanup commands
- Hardware detection helper
- Metrics endpoint

## Configuration

### Server Config (server.json)
```json
{
  "host": "10.0.0.6",
  "port": 30814,
  "max_queue_size": 50000,
  "workers_file": "workers.json",
  "jobs_log_file": "jobs.log"
}
```

### Client Config (client.json)
```json
{
  "server_host": "10.0.0.6",
  "server_port": 30814
}
```

**Hardcoded Values:**
- Worker timeout: 15 seconds
- Client poll backoff: 1s → 2s → 4s → 8s → 10s (max)

## Security Notes

**This system is designed for trusted home networks only:**
- No authentication
- Commands executed via `sh -c` (shell injection possible)
- No path traversal protection
- No input validation on file uploads
- No size limits on outputs

**Requirements:**
- Static IP addresses for all workers (DHCP reservations recommended)
- SSH keys configured for pdsh operations
- Trusted users only
