# -*- coding: utf-8 -*-
import asyncio, sys, json
sys.path.insert(0, r'C:\Users\Administrator\flow-agent\flow-agent')
from flow_server.state import get_active_bridge
from flow_engine.generators.common import build_client_context, build_generation_context

async def test():
    bridge = await get_active_bridge()
    project_id = '576adec2-b74d-4cd9-93da-ad81378711db'
    ref_ids = ['67ec01ae-a2ed-4e2f-bb4b-e6efae9b331e', 'dcced82d-fd45-4da4-9c8c-7aa3fdc71f62']
    prompt = 'Cinematic test portrait'
    
    req_item = {
        'clientContext': build_client_context(project_id),
        'seed': 123456,
        'imageModelName': 'IMAGE_MODEL_NARWHAL',
        'imageAspectRatio': 'IMAGE_ASPECT_RATIO_LANDSCAPE',
        'structuredPrompt': {'parts': [{'text': prompt}]},
        'imageInputs': [{'name': mid, 'imageInputType': 'IMAGE_INPUT_TYPE_REFERENCE'} for mid in ref_ids],
    }
    body = {
        'clientContext': build_client_context(project_id),
        'mediaGenerationContext': build_generation_context(),
        'useNewMedia': True,
        'requests': [req_item],
    }
    endpoint = f'/v1/projects/{project_id}/flowMedia:batchGenerateImages'
    
    print('Sending api_request to bridge with client-jhbh11...', flush=True)
    res = await bridge.api_request(endpoint, body, captcha_action='IMAGE_GENERATION', timeout=30, client_id='client-jhbh11')
    print('Raw response:', json.dumps(res, indent=2), flush=True)

if __name__ == '__main__':
    asyncio.run(test())
