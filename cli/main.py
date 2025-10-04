#!/usr/bin/env python3
"""C2 CLI - Interactive command-line interface for managing the C2 system."""
import sys
import json
import readline
import base64
import subprocess
import shutil
from pathlib import Path
from typing import Optional
import requests

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.models import JobSubmission

# Server connection
SERVER_URL = "http://10.0.0.6:30814"
HISTORY_FILE = Path.home() / ".c2_history"

# Load history
if HISTORY_FILE.exists():
    readline.read_history_file(HISTORY_FILE)


def save_history():
    """Save command history."""
    readline.write_history_file(HISTORY_FILE)


def try_server(url: str) -> bool:
    """Test if server is reachable."""
    try:
        response = requests.get(f"{url}/api/workers/list", timeout=2)
        return response.status_code == 200
    except:
        return False


def get_server_url() -> str:
    """Get server URL, trying hardcoded address first, then localhost."""
    if try_server(SERVER_URL):
        return SERVER_URL

    # Try localhost
    local_url = "http://localhost:30814"
    if try_server(local_url):
        print(f"Warning: Using localhost instead of {SERVER_URL}")
        return local_url

    print(f"Error: Cannot connect to server at {SERVER_URL} or localhost:30814")
    print("Please ensure the server is running and try again.")
    sys.exit(1)


def cmd_list(args):
    """List all registered workers."""
    response = requests.get(f"{SERVER_URL}/api/workers/list")
    response.raise_for_status()
    result = response.json()

    if not result["workers"]:
        print("No workers registered")
        return

    print(f"{'IP':<15} {'Hostname':<20} {'Status':<15} {'Groups':<30} {'Jobs':<10}")
    print("-" * 90)
    for worker in result["workers"]:
        groups = ",".join(worker["groups"]) if worker["groups"] else "-"
        num_jobs = len(worker["current_jobs"])
        print(f"{worker['ip']:<15} {worker['hostname']:<20} {worker['status']:<15} {groups:<30} {num_jobs:<10}")


def cmd_groups(args):
    """List all worker groups."""
    response = requests.get(f"{SERVER_URL}/api/workers/list")
    response.raise_for_status()
    result = response.json()

    groups = set()
    for worker in result["workers"]:
        groups.update(worker["groups"])

    if not groups:
        print("No groups defined")
        return

    print("Worker Groups:")
    for group in sorted(groups):
        # Count workers in this group
        count = sum(1 for w in result["workers"] if group in w["groups"])
        print(f"  {group} ({count} workers)")


def cmd_assign(args):
    """Assign worker to group: assign <ip> <group>"""
    if len(args) < 2:
        print("Usage: assign <ip> <group>")
        return

    ip, group = args[0], args[1]
    response = requests.post(
        f"{SERVER_URL}/api/workers/groups/{ip}",
        json={"groups": [group]}
    )
    response.raise_for_status()
    print(f"Assigned {ip} to group '{group}'")


def cmd_unassign(args):
    """Remove worker from group: unassign <ip> <group>"""
    if len(args) < 2:
        print("Usage: unassign <ip> <group>")
        return

    ip, group = args[0], args[1]
    response = requests.delete(f"{SERVER_URL}/api/workers/groups/{ip}/{group}")
    response.raise_for_status()
    print(f"Removed {ip} from group '{group}'")


def cmd_info(args):
    """Show detailed worker information: info <ip>"""
    if len(args) < 1:
        print("Usage: info <ip>")
        return

    ip = args[0]
    response = requests.get(f"{SERVER_URL}/api/workers/info/{ip}")
    response.raise_for_status()
    worker = response.json()

    print(f"Worker: {worker['hostname']} ({worker['ip']})")
    print(f"  OS: {worker['os']} {worker['arch']}")
    print(f"  Disk Available: {worker['disk_available_gb']:.2f} GB")
    print(f"  Status: {worker['status']}")
    print(f"  Groups: {', '.join(worker['groups']) if worker['groups'] else 'none'}")
    print(f"  Available Tags: {', '.join(worker['available_tags']) if worker['available_tags'] else 'none'}")

    if worker['tag_locks']:
        print("  Tag Locks:")
        for tag, job_id in worker['tag_locks'].items():
            status = job_id if job_id else "available"
            print(f"    {tag}: {status}")

    print(f"  Last Seen: {worker['last_seen']}")
    print(f"  Registered: {worker['registered_at']}")

    if worker['current_jobs']:
        print(f"  Current Jobs ({len(worker['current_jobs'])}):")
        for job in worker['current_jobs']:
            print(f"    {job['id']}: {job['command'][:60]}... ({job['status']})")
    else:
        print("  Current Jobs: none")


def cmd_set_tags(args):
    """Set available tags for worker: set-tags <ip> <tag1,tag2,...>"""
    if len(args) < 2:
        print("Usage: set-tags <ip> <tag1,tag2,...>")
        return

    ip = args[0]
    tags = args[1].split(",")
    response = requests.post(
        f"{SERVER_URL}/api/workers/tags/{ip}",
        json={"tags": tags}
    )
    response.raise_for_status()
    print(f"Set tags for {ip}: {', '.join(tags)}")


def cmd_get_tags(args):
    """Show worker tags: get-tags <ip>"""
    if len(args) < 1:
        print("Usage: get-tags <ip>")
        return

    ip = args[0]
    response = requests.get(f"{SERVER_URL}/api/workers/tags/{ip}")
    response.raise_for_status()
    result = response.json()

    print(f"Tags for {ip}:")
    print(f"  Available: {', '.join(result['available_tags']) if result['available_tags'] else 'none'}")
    if result['tag_locks']:
        print("  Locks:")
        for tag, job_id in result['tag_locks'].items():
            status = job_id if job_id else "available"
            print(f"    {tag}: {status}")


def cmd_jobs(args):
    """List all active jobs."""
    response = requests.get(f"{SERVER_URL}/api/jobs/list")
    response.raise_for_status()
    result = response.json()

    if not result["jobs"]:
        print("No active jobs")
        return

    print(f"{'Job ID':<40} {'Status':<12} {'Worker':<15} {'Command':<50}")
    print("-" * 120)
    for job in result["jobs"]:
        worker = job.get("assigned_worker") or "-"
        command = job["command"][:47] + "..." if len(job["command"]) > 50 else job["command"]
        print(f"{job['id']:<40} {job['status']:<12} {worker:<15} {command:<50}")


def cmd_job(args):
    """Show detailed job information: job <job_id>"""
    if len(args) < 1:
        print("Usage: job <job_id>")
        return

    job_id = args[0]
    response = requests.get(f"{SERVER_URL}/api/jobs/info/{job_id}")
    response.raise_for_status()
    job = response.json()

    print(f"Job: {job['id']}")
    print(f"  Command: {job['command']}")
    print(f"  Status: {job['status']}")
    print(f"  Group: {job.get('group') or 'any'}")
    print(f"  Tags: {', '.join(job['tags']) if job['tags'] else 'none'}")
    print(f"  Dependencies: {', '.join(job['dependencies']) if job['dependencies'] else 'none'}")
    print(f"  Same Machine: {job['same_machine']}")
    print(f"  Worker: {job.get('assigned_worker') or 'not assigned'}")
    print(f"  Created: {job['created_at']}")
    if job.get('started_at'):
        print(f"  Started: {job['started_at']}")
    if job.get('completed_at'):
        print(f"  Completed: {job['completed_at']}")
    if job.get('exit_code') is not None:
        print(f"  Exit Code: {job['exit_code']}")
    if job.get('stdout'):
        print(f"  Stdout: {job['stdout'][:200]}{'...' if len(job['stdout']) > 200 else ''}")
    if job.get('stderr'):
        print(f"  Stderr: {job['stderr'][:200]}{'...' if len(job['stderr']) > 200 else ''}")


def cmd_cancel(args):
    """Cancel a job: cancel <job_id>"""
    if len(args) < 1:
        print("Usage: cancel <job_id>")
        return

    job_id = args[0]
    response = requests.post(f"{SERVER_URL}/api/jobs/cancel/{job_id}")
    response.raise_for_status()
    print(f"Cancelled job {job_id}")


def cmd_queue_status(args):
    """Show queue size and capacity."""
    response = requests.get(f"{SERVER_URL}/api/jobs/queue-status")
    response.raise_for_status()
    status = response.json()

    print(f"Queue Status:")
    print(f"  Pending: {status['pending']}")
    print(f"  Running: {status['running']}")
    print(f"  Capacity: {status['capacity']}")
    print(f"  Available: {status['available']}")


def cmd_submit(args):
    """Submit a job: submit <command> [--group=<group>] [--depends=<job_ids>] [--same-machine] [--tags=<tags>] [--stdin=<string>] [--stdin-file=<path>] [--upload=<local:remote>] [--download=<pattern>] [--dry-run]"""
    if len(args) < 1:
        print("Usage: submit <command> [options]")
        return

    # Parse command and options
    command_parts = []
    options = {
        "group": None,
        "dependencies": [],
        "same_machine": False,
        "tags": [],
        "stdin": None,
        "input_files": {},
        "output_file_patterns": [],
        "dry_run": False
    }

    for arg in args:
        if arg.startswith("--"):
            if "=" in arg:
                key, value = arg[2:].split("=", 1)
                if key == "group":
                    options["group"] = value
                elif key == "depends":
                    options["dependencies"] = value.split(",")
                elif key == "tags":
                    options["tags"] = value.split(",")
                elif key == "stdin":
                    options["stdin"] = value
                elif key == "stdin-file":
                    try:
                        with open(value) as f:
                            options["stdin"] = f.read()
                    except FileNotFoundError:
                        print(f"Error: File not found: {value}")
                        return
                elif key == "upload":
                    if ":" not in value:
                        print(f"Error: Upload format should be local:remote, got: {value}")
                        return
                    local_path, remote_name = value.split(":", 1)
                    try:
                        with open(local_path, "rb") as f:
                            options["input_files"][remote_name] = base64.b64encode(f.read()).decode()
                    except FileNotFoundError:
                        print(f"Error: File not found: {local_path}")
                        return
                elif key == "download":
                    options["output_file_patterns"].append(value)
            elif arg == "--same-machine":
                options["same_machine"] = True
            elif arg == "--dry-run":
                options["dry_run"] = True
        else:
            command_parts.append(arg)

    command = " ".join(command_parts)

    if not command or not command.strip():
        print("Error: Command cannot be empty")
        return

    # Check queue capacity
    response = requests.get(f"{SERVER_URL}/api/jobs/queue-status")
    response.raise_for_status()
    status = response.json()

    if status["available"] < 1:
        print("Error: Queue is full")
        return

    if options["dry_run"]:
        print("Dry run - would submit:")
        print(f"  Command: {command}")
        print(f"  Group: {options['group'] or 'any'}")
        print(f"  Dependencies: {options['dependencies'] or 'none'}")
        print(f"  Same Machine: {options['same_machine']}")
        print(f"  Tags: {options['tags'] or 'none'}")
        return

    # Submit job
    submission = JobSubmission(
        command=command,
        group=options["group"],
        dependencies=options["dependencies"],
        same_machine=options["same_machine"],
        tags=options["tags"],
        stdin=options["stdin"],
        input_files=options["input_files"],
        output_file_patterns=options["output_file_patterns"]
    )

    response = requests.post(
        f"{SERVER_URL}/api/jobs/submit",
        json=submission.model_dump(exclude_none=True)
    )
    response.raise_for_status()
    result = response.json()
    print(f"Submitted job {result['job_id']}")


def cmd_log(args):
    """Show recent job results: log [n]"""
    lines = int(args[0]) if args else 50

    response = requests.get(f"{SERVER_URL}/api/jobs/log?lines={lines}")
    response.raise_for_status()
    result = response.json()

    if not result["entries"]:
        print("No job log entries")
        return

    # Format for less
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
        f.write(f"{'Job ID':<40} {'Status':<12} {'Exit':<5} {'Worker':<15} {'Command':<60}\n")
        f.write("-" * 135 + "\n")
        for entry in result["entries"]:
            command = entry["command"][:57] + "..." if len(entry["command"]) > 60 else entry["command"]
            exit_code = str(entry.get("exit_code", "-"))
            worker = entry.get("worker_hostname", "-")[:15]
            f.write(f"{entry['job_id']:<40} {entry['status']:<12} {exit_code:<5} {worker:<15} {command:<60}\n")
        temp_file = f.name

    try:
        subprocess.run(["less", "-S", temp_file])
    finally:
        Path(temp_file).unlink()


def check_pdsh_installed() -> bool:
    """Check if pdsh is installed."""
    return shutil.which("pdsh") is not None


def install_pdsh():
    """Attempt to install pdsh."""
    print("pdsh not found. Attempting to install...")
    try:
        result = subprocess.run(
            ["sudo", "apt", "install", "-y", "pdsh"],
            capture_output=True,
            text=True,
            timeout=60
        )
        if result.returncode == 0:
            print("pdsh installed successfully")
            return True
        else:
            print(f"Failed to install pdsh: {result.stderr}")
            return False
    except Exception as e:
        print(f"Failed to install pdsh: {e}")
        return False


def cmd_pdsh(args):
    """Execute pdsh command: pdsh <target> <command>"""
    if len(args) < 2:
        print("Usage: pdsh <target> <command>")
        print("  <target> can be: @group, @all, or IP address")
        return

    # Check if pdsh is installed
    if not check_pdsh_installed():
        if not install_pdsh():
            print("Error: pdsh is required but could not be installed")
            return
        # Verify installation
        if not check_pdsh_installed():
            print("Error: pdsh installation failed")
            return

    target = args[0]
    command = " ".join(args[1:])

    # Get worker list from server
    response = requests.get(f"{SERVER_URL}/api/pdsh/workers/{target}")
    response.raise_for_status()
    result = response.json()

    if not result["workers"]:
        print(f"No workers found for target '{target}'")
        return

    hostnames = result["hostnames"]

    # Execute pdsh
    print(f"Executing on {len(result['workers'])} worker(s): {hostnames}")
    try:
        pdsh_result = subprocess.run(
            ["pdsh", "-w", hostnames, command],
            capture_output=False,
            text=True
        )
    except Exception as e:
        print(f"Error executing pdsh: {e}")


def cmd_verify_ssh(args):
    """Verify SSH connectivity: verify-ssh <target>"""
    if len(args) < 1:
        print("Usage: verify-ssh <target>")
        print("  <target> can be: @group, @all, or IP address")
        return

    target = args[0]

    # Get worker list from server
    response = requests.get(f"{SERVER_URL}/api/pdsh/workers/{target}")
    response.raise_for_status()
    result = response.json()

    if not result["workers"]:
        print(f"No workers found for target '{target}'")
        return

    print(f"Verifying SSH connectivity to {len(result['workers'])} worker(s)...")

    for worker in result["workers"]:
        ip = worker["ip"]
        hostname = worker["hostname"]
        try:
            ssh_result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", ip, "echo ok"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if ssh_result.returncode == 0 and "ok" in ssh_result.stdout:
                print(f"  ✓ {hostname} ({ip})")
            else:
                print(f"  ✗ {hostname} ({ip}) - SSH failed")
        except Exception as e:
            print(f"  ✗ {hostname} ({ip}) - {e}")


def cmd_batch(args):
    """Submit batch jobs from file: batch <file> [--dry-run]"""
    if len(args) < 1:
        print("Usage: batch <file> [--dry-run]")
        return

    file_path = args[0]
    dry_run = "--dry-run" in args

    if not Path(file_path).exists():
        print(f"Error: File not found: {file_path}")
        return

    # Read batch file
    with open(file_path) as f:
        lines = [line.strip() for line in f if line.strip()]

    # Parse each line as JSON or plain command
    submissions = []
    for i, line in enumerate(lines, 1):
        try:
            # Try to parse as JSON
            job_data = json.loads(line)
            submissions.append(JobSubmission(**job_data))
        except json.JSONDecodeError:
            # Treat as plain command
            submissions.append(JobSubmission(command=line))
        except Exception as e:
            print(f"Error parsing line {i}: {e}")
            print(f"  Line: {line}")
            return

    # Check queue capacity
    response = requests.get(f"{SERVER_URL}/api/jobs/queue-status")
    response.raise_for_status()
    status = response.json()

    if status["available"] < len(submissions):
        print(f"Error: Batch would exceed queue capacity ({len(submissions)} jobs, {status['available']} available)")
        return

    if dry_run:
        print(f"Dry run - would submit {len(submissions)} jobs:")
        for i, sub in enumerate(submissions, 1):
            print(f"  {i}. {sub.command[:60]}... (group={sub.group or 'any'})")
        return

    # Submit batch
    response = requests.post(
        f"{SERVER_URL}/api/jobs/submit-batch",
        json={"jobs": [s.model_dump(exclude_none=True) for s in submissions]}
    )
    response.raise_for_status()
    result = response.json()

    print(f"Submitted {len(result['job_ids'])} jobs")
    for job_id in result["job_ids"]:
        print(f"  {job_id}")


def cmd_split(args):
    """Split work across workers: split <command_template> <input_file> [--group=<group>] [--dry-run]"""
    if len(args) < 2:
        print("Usage: split <command_template> <input_file> [--group=<group>] [--dry-run]")
        print("  Command template uses {} as placeholder")
        return

    # Parse arguments
    command_template = args[0]
    input_file = args[1]
    group = None
    dry_run = False

    for arg in args[2:]:
        if arg.startswith("--group="):
            group = arg.split("=", 1)[1]
        elif arg == "--dry-run":
            dry_run = True

    if not Path(input_file).exists():
        print(f"Error: File not found: {input_file}")
        return

    # Read input lines
    with open(input_file) as f:
        lines = [line.strip() for line in f if line.strip()]

    # Create job submissions
    submissions = []
    for line in lines:
        command = command_template.replace("{}", line)
        submissions.append(JobSubmission(command=command, group=group))

    # Check queue capacity
    response = requests.get(f"{SERVER_URL}/api/jobs/queue-status")
    response.raise_for_status()
    status = response.json()

    if status["available"] < len(submissions):
        print(f"Error: Split would exceed queue capacity ({len(submissions)} jobs, {status['available']} available)")
        return

    if dry_run:
        print(f"Dry run - would submit {len(submissions)} jobs:")
        for i, sub in enumerate(submissions[:5], 1):
            print(f"  {i}. {sub.command}")
        if len(submissions) > 5:
            print(f"  ... and {len(submissions) - 5} more")
        return

    # Submit batch
    response = requests.post(
        f"{SERVER_URL}/api/jobs/submit-batch",
        json={"jobs": [s.model_dump(exclude_none=True) for s in submissions]}
    )
    response.raise_for_status()
    result = response.json()

    print(f"Submitted {len(result['job_ids'])} jobs")


def cmd_get_files(args):
    """Download job output files: get-files <job_id> [output_dir]"""
    if len(args) < 1:
        print("Usage: get-files <job_id> [output_dir]")
        return

    job_id = args[0]
    output_dir = Path(args[1]) if len(args) > 1 else Path.cwd()

    try:
        response = requests.get(f"{SERVER_URL}/api/jobs/files/{job_id}")
        response.raise_for_status()
        result = response.json()

        if not result["files"]:
            print(f"No output files for job {job_id}")
            return

        output_dir.mkdir(parents=True, exist_ok=True)

        print(f"Downloading {len(result['files'])} file(s) to {output_dir}...")
        for filename, b64_content in result["files"].items():
            try:
                file_path = output_dir / filename
                file_path.write_bytes(base64.b64decode(b64_content))
                print(f"  {filename}")
            except Exception as e:
                print(f"  Error saving {filename}: {e}")

        print("Download complete")
    except requests.exceptions.HTTPException as e:
        if hasattr(e.response, 'status_code') and e.response.status_code == 410:
            print(f"Job {job_id} has completed. Output files are no longer available.")
            print("Files are only available for active/running jobs.")
        else:
            raise


def cmd_cleanup_workspaces(args):
    """Trigger workspace cleanup on workers: cleanup-workspaces <ip|@all>"""
    if len(args) < 1:
        print("Usage: cleanup-workspaces <ip|@all>")
        return

    target = args[0]

    if target == "@all":
        # Get all workers
        response = requests.get(f"{SERVER_URL}/api/workers/list")
        response.raise_for_status()
        result = response.json()
        workers = [w["ip"] for w in result["workers"]]
    else:
        workers = [target]

    print(f"Triggering cleanup on {len(workers)} worker(s)...")
    for ip in workers:
        try:
            response = requests.post(f"{SERVER_URL}/api/workers/cleanup/{ip}")
            response.raise_for_status()
            print(f"  ✓ {ip}")
        except Exception as e:
            print(f"  ✗ {ip}: {e}")


def cmd_detect_tags(args):
    """Run hardware detection and suggest tags: detect-tags <ip>"""
    if len(args) < 1:
        print("Usage: detect-tags <ip>")
        return

    ip = args[0]

    try:
        response = requests.post(f"{SERVER_URL}/api/workers/detect-tags/{ip}")
        response.raise_for_status()
        result = response.json()

        print(f"Suggested tags for {ip}:")
        for tag in result["suggested_tags"]:
            print(f"  {tag}")

        if result["suggested_tags"]:
            print(f"\nTo apply these tags, run:")
            print(f"  set-tags {ip} {','.join(result['suggested_tags'])}")
    except Exception as e:
        print(f"Error: {e}")


def cmd_help(args):
    """Show help message."""
    print("""
C2 Command & Control CLI

Worker Management:
  list                          - List all registered workers
  groups                        - List all worker groups
  assign <ip> <group>           - Add worker to group
  unassign <ip> <group>         - Remove worker from group
  info <ip>                     - Show detailed worker information
  set-tags <ip> <tag1,tag2,...> - Set available tags for worker
  get-tags <ip>                 - Show worker tag status
  cleanup-workspaces <ip|@all>  - Trigger workspace cleanup on workers
  detect-tags <ip>              - Run hardware detection and suggest tags

Job Management:
  jobs                          - List all active jobs
  job <job_id>                  - Show detailed job information
  cancel <job_id>               - Cancel a job
  queue-status                  - Show queue size and capacity
  log [n]                       - Show last n job results (default: 50)
  get-files <job_id> [dir]      - Download job output files
  submit <command> [options]    - Submit a new job
    Options:
      --group=<group>             - Target worker group
      --depends=<job1,job2>       - Comma-separated dependencies
      --same-machine              - Run on same machine as dependencies
      --tags=<tag1,tag2>          - Required tags/locks
      --stdin=<string>            - Stdin data
      --stdin-file=<path>         - Read stdin from file
      --upload=<local:remote>     - Upload file for job
      --download=<pattern>        - Output file pattern
      --dry-run                   - Validate without submitting

Batch Operations:
  batch <file> [--dry-run]      - Submit jobs from file (JSON or plain commands)
  split <template> <file> [--group=<group>] [--dry-run]
                                - Split work using {} placeholder

PDSH Integration:
  pdsh <target> <command>       - Execute command via pdsh
  verify-ssh <target>           - Verify SSH connectivity
    <target> can be: @group, @all, or IP address

General:
  help                          - Show this help
  exit, quit                    - Exit CLI
""")


def main():
    """Main CLI loop."""
    global SERVER_URL

    # Test server connection
    SERVER_URL = get_server_url()

    print("C2 Command & Control CLI")
    print(f"Connected to {SERVER_URL}")
    print("Type 'help' for available commands\n")

    commands = {
        "list": cmd_list,
        "groups": cmd_groups,
        "assign": cmd_assign,
        "unassign": cmd_unassign,
        "info": cmd_info,
        "set-tags": cmd_set_tags,
        "get-tags": cmd_get_tags,
        "cleanup-workspaces": cmd_cleanup_workspaces,
        "detect-tags": cmd_detect_tags,
        "jobs": cmd_jobs,
        "job": cmd_job,
        "cancel": cmd_cancel,
        "queue-status": cmd_queue_status,
        "submit": cmd_submit,
        "log": cmd_log,
        "get-files": cmd_get_files,
        "batch": cmd_batch,
        "split": cmd_split,
        "pdsh": cmd_pdsh,
        "verify-ssh": cmd_verify_ssh,
        "help": cmd_help,
    }

    while True:
        try:
            line = input("!> ").strip()
            if not line:
                continue

            parts = line.split()
            cmd = parts[0]
            args = parts[1:]

            if cmd in ["exit", "quit"]:
                break

            if cmd in commands:
                try:
                    commands[cmd](args)
                except requests.exceptions.RequestException as e:
                    print(f"Error: {e}")
                except Exception as e:
                    print(f"Error: {e}")
            else:
                print(f"Unknown command: {cmd}. Type 'help' for available commands.")

        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            continue

    save_history()


if __name__ == "__main__":
    main()
