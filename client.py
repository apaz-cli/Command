#!/usr/bin/env python3
"""
C2 CLI - Command-line interface for managing the C2 server
"""

import cmd
import requests
import json
import sys
from typing import List, Dict
from tabulate import tabulate

class C2CLI(cmd.Cmd):
    intro = "C2 Command and Control CLI. Type 'help' for commands."
    prompt = "c2> "
    
    def __init__(self, server_url: str = "http://localhost:8000"):
        super().__init__()
        self.server_url = server_url
        self.selected_clients: List[str] = []
        
    def do_status(self, arg):
        """Check server status"""
        try:
            resp = requests.get(f"{self.server_url}/")
            data = resp.json()
            print(f"Service: {data['service']}")
            print(f"Status: {data['status']}")
            print(f"Connected clients: {data['clients_connected']}")
        except Exception as e:
            print(f"Error: {e}")
    
    def do_list(self, arg):
        """List all registered clients"""
        try:
            resp = requests.get(f"{self.server_url}/clients")
            clients = resp.json()["clients"]
            
            if not clients:
                print("No clients registered")
                return
            
            table_data = []
            for client in clients:
                table_data.append([
                    client['id'][:8],
                    client['hostname'],
                    client['platform'],
                    client['ip_address'],
                    client['status']
                ])
            
            print(tabulate(
                table_data,
                headers=['ID', 'Hostname', 'Platform', 'IP', 'Status'],
                tablefmt='grid'
            ))
        except Exception as e:
            print(f"Error: {e}")
    
    def do_select(self, arg):
        """Select client(s) for operations. Usage: select <client_id> [client_id2 ...]"""
        if not arg:
            print("Usage: select <client_id> [client_id2 ...]")
            return
        
        client_ids = arg.split()
        
        # Verify clients exist
        try:
            resp = requests.get(f"{self.server_url}/clients")
            all_clients = {c['id']: c for c in resp.json()["clients"]}
            
            valid_ids = []
            for cid in client_ids:
                # Support short IDs
                matches = [full_id for full_id in all_clients.keys() if full_id.startswith(cid)]
                if matches:
                    valid_ids.extend(matches)
                else:
                    print(f"Client {cid} not found")
            
            self.selected_clients = valid_ids
            print(f"Selected {len(self.selected_clients)} client(s)")
            for cid in self.selected_clients:
                print(f"  - {all_clients[cid]['hostname']} ({cid[:8]})")
                
        except Exception as e:
            print(f"Error: {e}")
    
    def do_select_all(self, arg):
        """Select all connected clients"""
        try:
            resp = requests.get(f"{self.server_url}/clients")
            clients = resp.json()["clients"]
            self.selected_clients = [c['id'] for c in clients if c['status'] == 'connected']
            print(f"Selected {len(self.selected_clients)} connected client(s)")
        except Exception as e:
            print(f"Error: {e}")
    
    def do_exec(self, arg):
        """Execute command on selected clients. Usage: exec <command>"""
        if not self.selected_clients:
            print("No clients selected. Use 'select' first.")
            return
        
        if not arg:
            print("Usage: exec <command>")
            return
        
        try:
            resp = requests.post(
                f"{self.server_url}/execute",
                json={
                    "client_ids": self.selected_clients,
                    "command": arg,
                    "timeout": 30
                }
            )
            
            results = resp.json()["results"]
            
            for client_id, result in results.items():
                print(f"\n{'='*60}")
                print(f"Client: {client_id[:8]}")
                print(f"{'='*60}")
                
                if "error" in result:
                    print(f"Error: {result['error']}")
                else:
                    cmd_result = result.get("result", {})
                    if cmd_result.get("success"):
                        print(f"Return code: {cmd_result['returncode']}")
                        if cmd_result['stdout']:
                            print(f"\nStdout:\n{cmd_result['stdout']}")
                        if cmd_result['stderr']:
                            print(f"\nStderr:\n{cmd_result['stderr']}")
                    else:
                        print(f"Error: {cmd_result.get('error', 'Unknown error')}")
                        
        except Exception as e:
            print(f"Error: {e}")
    
    def do_queue_add(self, arg):
        """Add work to queue. Usage: queue_add <queue_name> <command>"""
        parts = arg.split(maxsplit=1)
        if len(parts) < 2:
            print("Usage: queue_add <queue_name> <command>")
            return
        
        queue_name, command = parts
        
        # For demo, create a simple work item
        work_item = {
            "task_id": f"task_{len(command)}",
            "command": command,
            "args": {}
        }
        
        try:
            resp = requests.post(
                f"{self.server_url}/work/queue",
                params={"queue_name": queue_name},
                json={"items": [work_item]}
            )
            
            data = resp.json()
            print(f"Added work to queue '{queue_name}'")
            print(f"Total items in queue: {data['total_items']}")
        except Exception as e:
            print(f"Error: {e}")
    
    def do_queue_show(self, arg):
        """Show queue contents. Usage: queue_show <queue_name>"""
        if not arg:
            print("Usage: queue_show <queue_name>")
            return
        
        try:
            resp = requests.get(f"{self.server_url}/work/queue/{arg}")
            data = resp.json()
            
            print(f"Queue: {data['queue_name']}")
            print(f"Items: {data['count']}")
            
            if data['items']:
                for item in data['items']:
                    print(f"  - {item['task_id']}: {item['command']}")
        except Exception as e:
            print(f"Error: {e}")
    
    def do_queue_distribute(self, arg):
        """Distribute queue work across clients. Usage: queue_distribute <queue_name>"""
        if not arg:
            print("Usage: queue_distribute <queue_name>")
            return
        
        try:
            resp = requests.post(f"{self.server_url}/work/distribute/{arg}")
            data = resp.json()
            
            print(f"Work distributed to {data['clients']} client(s)")
            for client_id, count in data['items_per_client'].items():
                print(f"  - {client_id[:8]}: {count} items")
        except Exception as e:
            print(f"Error: {e}")
    
    def do_remove(self, arg):
        """Remove a client. Usage: remove <client_id>"""
        if not arg:
            print("Usage: remove <client_id>")
            return
        
        try:
            # Find full client ID
            resp = requests.get(f"{self.server_url}/clients")
            all_clients = {c['id']: c for c in resp.json()["clients"]}
            matches = [full_id for full_id in all_clients.keys() if full_id.startswith(arg)]
            
            if not matches:
                print(f"Client {arg} not found")
                return
            
            client_id = matches[0]
            resp = requests.delete(f"{self.server_url}/clients/{client_id}")
            print(f"Removed client {client_id[:8]}")
            
            # Remove from selection if present
            if client_id in self.selected_clients:
                self.selected_clients.remove(client_id)
                
        except Exception as e:
            print(f"Error: {e}")
    
    def do_exit(self, arg):
        """Exit the CLI"""
        print("Goodbye!")
        return True
    
    def do_quit(self, arg):
        """Exit the CLI"""
        return self.do_exit(arg)
    
    def emptyline(self):
        """Do nothing on empty line"""
        pass

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="C2 CLI")
    parser.add_argument(
        "--server",
        default="http://localhost:8000",
        help="C2 server URL (default: http://localhost:8000)"
    )
    
    args = parser.parse_args()
    
    cli = C2CLI(args.server)
    cli.cmdloop()

if __name__ == "__main__":
    main()
