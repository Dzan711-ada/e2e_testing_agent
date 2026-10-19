"""Shared provider transport, structured output, correction and call budgets."""
import json
import time
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError


class ModelGateway:
    def __init__(self, settings, *, max_calls=40, redact=lambda value: value):
        settings.require_model()
        self.settings, self.max_calls, self.redact = settings, max_calls, redact
        self.calls, self.inputs, self.providers = [], [], []
        for label, key, model, base in (
            ('DeepSeek', settings.deepseek_api_key, settings.deepseek_model, settings.deepseek_base_url),
            ('Qwen', settings.qwen_api_key, settings.qwen_model, settings.qwen_base_url)):
            if key:
                self.providers.append((label, ChatOpenAI(model=model, api_key=key, base_url=base,
                    temperature=0, timeout=settings.request_timeout, max_retries=0)))

    async def call(self, role, schema, prompt, payload):
        errors = []
        feedback = []
        for label, client in self.providers:
            for correction in range(2):
                if len(self.calls) >= self.max_calls:
                    raise RuntimeError('Model call budget exhausted')
                started = time.monotonic()
                method = 'function_calling' if role == 'execute' else 'json_mode'
                event = {'role': role, 'provider': label, 'status': 'running', 'structured_method': method, 'correction': correction}
                self.calls.append(event)
                safe = self.redact({**payload, **({'previous_validation_errors': feedback} if feedback else {})})
                self.inputs.append({'role': role, 'payload': safe})
                try:
                    instructions = prompt
                    if method == 'json_mode':
                        instructions += '\nReturn a complete JSON INSTANCE matching this schema:\n' + json.dumps(schema.model_json_schema())
                    if feedback:
                        instructions += '\nCorrect the structural errors using the provided feedback. Preserve business scope and expectations; do not turn a clear request into clarification to evade schema validation.'
                    result = await client.with_structured_output(schema, method=method, include_raw=True).ainvoke([
                        SystemMessage(content=instructions), HumanMessage(content=json.dumps(safe, ensure_ascii=False))])
                    if result.get('parsing_error') or result.get('parsed') is None:
                        raise ValueError('Structured output was not accepted') from result.get('parsing_error')
                    output = schema.model_validate(result['parsed'])
                    event.update(status='passed', usage=getattr(result.get('raw'), 'usage_metadata', None))
                    return output
                except Exception as error:
                    # Never log raw completions or rejected values.
                    event.update(status='error', error=type(error).__name__)
                    pending, seen, feedback = [error], set(), []
                    while pending and len(seen) < 12:
                        cause = pending.pop()
                        if id(cause) in seen:
                            continue
                        seen.add(id(cause))
                        if isinstance(cause, ValidationError):
                            feedback = [{'path': '.'.join(map(str, item['loc'])), 'type': item['type'], 'message': item['msg']}
                                for item in cause.errors(include_input=False, include_context=False, include_url=False)[:10]]
                            event['validation_errors'] = feedback
                            break
                        pending.extend(e for e in (getattr(cause, '__cause__', None), getattr(cause, '__context__', None)) if e is not None)
                    errors.append(label + ': ' + type(error).__name__)
                    if not feedback:
                        break
                finally:
                    event['seconds'] = round(time.monotonic() - started, 3)
        raise RuntimeError('Model request failed: ' + '; '.join(errors))
