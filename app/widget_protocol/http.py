"""Authenticated protocol transport; no confirmation authority from client JSON."""
import json
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from .core import ExecutionContext, MAX_REQUEST_BYTES, WidgetRegistry
from .models import Capability, WidgetEvent, WidgetRequest, WidgetResponse

HTTP_STATUS = {'success': 200, 'needs_confirmation': 409, 'needs_clarification': 422,
               'not_found': 404, 'invalid_request': 400, 'permission_denied': 403,
               'conflict': 409, 'unavailable': 503, 'error': 500, 'accepted': 202, 'running': 202}


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _bad_constant(_):
    raise ValueError('Non-finite JSON number')


def register_routes(app, registry: WidgetRegistry, admin):
    @app.get('/api/widget-protocol/schema')
    async def schema(_=Depends(admin)):
        return {'protocol_version': '1.0', 'max_request_bytes': MAX_REQUEST_BYTES,
                'request': WidgetRequest.model_json_schema(), 'response': WidgetResponse.model_json_schema(),
                'capability': Capability.model_json_schema(), 'event': WidgetEvent.model_json_schema()}

    @app.get('/api/widget-protocol/widgets')
    async def widgets(_=Depends(admin)):
        return registry.manifest()

    @app.get('/api/widget-protocol/widgets/{name}')
    async def widget(name: str, _=Depends(admin)):
        entry = next((x for x in registry.manifest()['widgets'] if x['name'] == name), None)
        if entry is None:
            response = registry.failure(None, 'not_found', 'WIDGET_NOT_FOUND', '등록된 서비스가 없습니다.')
            return JSONResponse(response.model_dump(mode='json'), status_code=404)
        return {'protocol_version': '1.0', **entry}

    @app.get('/api/widget-protocol/health')
    async def health(_=Depends(admin)):
        authority = ExecutionContext(principal='admin', role='admin', permissions=frozenset({'read'}))
        results = {name: await registry.get(name).health(authority) for name in registry.list_widgets()}
        return {'protocol_version': '1.0', 'widgets': results}

    @app.post('/api/widget-protocol/requests')
    async def execute(request: Request):
        # Preserve admin/session/CSRF behavior; display and ingest credentials gain no access.
        try:
            admin(request)
        except HTTPException:
            response = registry.failure(None, 'permission_denied', 'AUTHENTICATION_REQUIRED', '관리자 인증이 필요합니다.')
            return JSONResponse(response.model_dump(mode='json'), status_code=401)
        content = await request.body()
        if len(content) > MAX_REQUEST_BYTES:
            response = registry.failure(None, 'invalid_request', 'REQUEST_TOO_LARGE', '요청은 64KiB 이하로 제한합니다.')
            return JSONResponse(response.model_dump(mode='json'), status_code=413)
        try:
            value = json.loads(content, object_pairs_hook=_json_object, parse_constant=_bad_constant)
        except (ValueError, UnicodeError, RecursionError):
            response = registry.failure(None, 'invalid_request', 'INVALID_JSON', '유효한 JSON 요청을 사용하세요.')
            return JSONResponse(response.model_dump(mode='json'), status_code=400)
        # A manager can validate all action schemas, but cannot approve execution by
        # self-asserting user_confirmed or by copying the public request digest.
        authority = ExecutionContext(principal='admin', role='admin',
                                     permissions=frozenset({'read', 'write', 'control', 'dangerous'}))
        response = await registry.execute(value, authority)
        return JSONResponse(response.model_dump(mode='json'), status_code=HTTP_STATUS[response.status])
