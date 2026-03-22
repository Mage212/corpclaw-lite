import json
import logging
import sys

from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.security.ipc_auth import IPCAuth

# In a minimal skeleton, we just mock the registry loader
# Real implementations would load configured tools
registry = ToolRegistry()

logging.basicConfig(level=logging.ERROR)

def process_request() -> None:
    """Read from stdin, verify, execute tool, sign response, print to stdout."""
    # 1. Read input
    input_data = sys.stdin.read()
    if not input_data:
        return
        
    try:
        req = json.loads(input_data)
        
        # 2. Verify
        # In the container environment we assume CORPCLAW_IPC_SECRET is injected
        auth = IPCAuth()
        payload = auth.verify(req)
        
        if payload.get("type") != "tool_call":
            raise ValueError("Unknown payload type")
            
        tool_name = payload.get("tool")
        args = payload.get("args", {})
        
        # 3. Execute
        # Here we mock the async execution
        # result = asyncio.run(registry.execute(tool_name, args))
        result = f"Mock execution of {tool_name} with {args} inside container"
        
        response_payload = {
            "status": "success",
            "result": result
        }
        
    except Exception as e:
        response_payload = {
            "status": "error",
            "error": str(e)
        }
        
    finally:
        # 4. Sign and respond
        try:
            auth = IPCAuth()
            signed_resp = auth.sign(response_payload)
            print(json.dumps(signed_resp))
        except Exception:
            # Fatal error, can't even sign the error
            print(json.dumps({
                "signature": "", "nonce": "", "timestamp": 0, 
                "payload": {"status": "error", "error": "Fatal IPC auth error in container"}
            }))

if __name__ == "__main__":
    process_request()
