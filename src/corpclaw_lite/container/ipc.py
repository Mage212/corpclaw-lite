import asyncio
import json
import logging
from typing import Any

from corpclaw_lite.security.ipc_auth import IPCAuth

logger = logging.getLogger(__name__)


class ContainerIPCError(Exception):
    """Raised for errors in container IPC communication."""
    pass


class ContainerIPC:
    """Manages stdio-based IPC with a running Docker container."""

    def __init__(
        self, 
        container_name: str, 
        auth: IPCAuth,
        timeout_seconds: float = 30.0
    ):
        self.container_name = container_name
        self.auth = auth
        self.timeout = timeout_seconds
        
    async def send_tool_call(self, tool_name: str, args: dict[str, Any]) -> str:
        """
        Send a tool execution request to the container and return the result.
        Uses docker exec to run a one-shot process that reads stdin, 
        authenticates, executes the tool, and prints to stdout.
        """
        payload = {
            "type": "tool_call",
            "tool": tool_name,
            "args": args
        }
        
        signed_message = self.auth.sign(payload)
        input_data = json.dumps(signed_message).encode('utf-8')
        
        # We use docker CLI directly via asyncio.create_subprocess_exec for simplicity in this phase.
        # Alternatively, docker SDK exec_run could be used, but it's blocking.
        cmd = [
            "docker", "exec", "-i", self.container_name, 
            "python", "-m", "corpclaw_lite.container.agent_worker"
        ]
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=input_data), 
                timeout=self.timeout
            )
            
            if process.returncode != 0:
                err_msg = stderr.decode('utf-8').strip()
                logger.error(f"ContainerIPC error executing {tool_name}: {err_msg}")
                return f"Container execution error: {err_msg}"
                
            # Parse response and verify it
            try:
                response_str = stdout.decode('utf-8').strip()
                response_msg = json.loads(response_str)
                verified_response = self.auth.verify(response_msg)
                
                if verified_response.get("status") == "error":
                    return f"Error from container: {verified_response.get('error')}"
                    
                return str(verified_response.get("result", ""))
                
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse container response: '{stdout.decode('utf-8')}'")
                return f"Error: Invalid JSON response from container: {e}"
            except Exception as e:
                logger.error(f"Container signature verification failed: {e}")
                return "Error: Security verification failed for container response."
                
        except TimeoutError:
            return f"Error: Container tool execution timed out after {self.timeout}s"
        except Exception as e:
            return f"Error in Container IPC: {e}"
