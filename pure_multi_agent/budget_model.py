from langchain_anthropic import ChatAnthropic
from langchain_core.callbacks import BaseCallbackHandler
from django_api.chat_policy import current_budget, metered_call, count_multimodal_input


class BudgetedChatAnthropic(ChatAnthropic):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        parent = super()._generate
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        counted = count_multimodal_input(self._client.messages, payload)
        return metered_call(self.model, payload, self.max_tokens,
            lambda: parent(messages, stop=stop, run_manager=run_manager, **kwargs), counted_input=counted)


class BudgetCallbacks(BaseCallbackHandler):
    raise_error = True
    run_inline = True

    def on_tool_start(self, *args, **kwargs):
        budget = current_budget.get()
        if budget:
            budget.tool()
