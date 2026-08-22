import sys
from pathlib import Path 
current_file=Path(__file__)
project_root=current_file.parent.parent
sys.path.insert(0,str(project_root))

from call_llm.client_factory import create_llm_client
from data_transmission.requirement import requirement_schema

client = create_llm_client(timeout=60)

def generate_requirement(messages):
    result=client.generate(messages=messages,response_schema=requirement_schema)
    return result

