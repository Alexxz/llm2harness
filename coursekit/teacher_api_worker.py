"""One bounded Responses API call; credentials come only from the process environment."""
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise ValueError('Redirect disabled')


def call(request):
    endpoint = os.environ.get('COURSE_TEACHER_API_ENDPOINT', 'https://api.openai.com/v1/responses')
    parsed = urlsplit(endpoint)
    local = parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
    if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and local)
            or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('Invalid API endpoint')
    key = os.environ.get('OPENAI_API_KEY')
    if not key:
        raise ValueError('Missing API key')
    data = {'model': request['model'], 'instructions': request['instructions'], 'input': request['input'],
            'tools': [], 'tool_choice': 'none', 'store': False, 'max_output_tokens': 4096,
            'text': {'format': {'type': 'json_schema', 'name': 'course_teacher_reply',
                                'strict': True, 'schema': request['schema']}}}
    message = urllib.request.Request(endpoint, data=json.dumps(data, ensure_ascii=False).encode(),
              headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method='POST')
    proxy = request.get('proxy')
    handlers = [urllib.request.ProxyHandler({'https': proxy, 'http': proxy} if proxy else {}), NoRedirect()]
    with urllib.request.build_opener(*handlers).open(message, timeout=request['timeout']) as response:
        body = response.read(2*1024*1024+1)
    if len(body) > 2*1024*1024:
        raise ValueError('Response too large')
    result = json.loads(body)
    if result.get('status') != 'completed' or result.get('error'):
        raise ValueError('Incomplete response')
    output = result.get('output', [])
    if any(item.get('type') not in {'reasoning', 'message'} for item in output):
        raise ValueError('Unexpected tool response')
    texts = [part['text'] for item in output if item.get('type') == 'message'
             for part in item.get('content', []) if part.get('type') == 'output_text']
    if not texts:
        raise ValueError('Empty response')
    return {'text': ''.join(texts), 'model': result.get('model'), 'usage': result.get('usage')}


def main():
    try:
        request = json.loads(sys.stdin.buffer.read(1024*1024))
        result = call(request)
    except Exception as error:
        result = {'error': type(error).__name__,
                  'status_code': error.code if isinstance(error, urllib.error.HTTPError) else None}
    print(json.dumps(result, ensure_ascii=False))
    return 1 if 'error' in result else 0


if __name__ == '__main__':
    raise SystemExit(main())
