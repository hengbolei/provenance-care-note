"""Shared fakes for agent tests."""

from carenote.agent.gateway import DataTag, ModelResult, Usage
from carenote.agent.state import CheckPoint, ReferenceCitation, ReviewPlan


class ScriptedGateway:
    """Drafts claims from the entry text it is shown, the way a well-behaved model would."""

    def __init__(self, tokens=(900, 300), plan_questions=()):
        self.requests = []
        self.plan_requests = []
        self.tokens = tokens
        # (question, query) pairs the planning call returns; it costs no tokens.
        self.plan_questions = plan_questions

    def structured(self, request, schema):
        if schema is ReviewPlan:
            self.plan_requests.append(request)
            questions = [{"question": q, "query": query} for q, query in self.plan_questions]
            return ModelResult(
                output=ReviewPlan.model_validate({"questions": questions}),
                model="scripted",
                usage=Usage(),
            )
        self.requests.append(request)
        claims = []
        highlights = []
        for quote, text in (
            ("Hold morning lisinopril", "Morning lisinopril is on hold pending review."),
            ("started lisinopril 10 mg", "Lisinopril 10 mg was started a week ago."),
        ):
            entry_id, start = _find(request, quote)
            citation = {"entry_id": entry_id, "start": start, "end": start + len(quote)}
            citation["quote"] = quote
            claims.append(
                {
                    "text": text,
                    "category": "medication",
                    "risk_level": "moderate",
                    "citations": [citation],
                }
            )
            if not highlights:
                highlights.append(
                    {
                        "citation": citation,
                        "risk_reason": "Antihypertensive on hold",
                        "suggested_action": "Recheck blood pressure before restarting",
                    }
                )
        draft = {"claims": claims, "open_questions": [], "candidate_highlights": highlights}
        return ModelResult(
            output=schema.model_validate(draft),
            model="scripted",
            usage=Usage(input_tokens=self.tokens[0], output_tokens=self.tokens[1]),
        )

    def text(self, request):
        raise AssertionError("the review graph only makes structured calls")


class CheckPointGateway(ScriptedGateway):
    """Adds one check point quoting the first reference passage it is shown."""

    def __init__(self, texts, quote=None):
        super().__init__()
        self.texts = list(texts)
        self.quote = quote

    def structured(self, request, schema):
        result = super().structured(request, schema)
        if schema is ReviewPlan:
            return result
        reference = None
        for item in request.inputs:
            if isinstance(item, DataTag) and item.name == "reference" and not item.closing:
                reference = dict(item.attributes)
            elif reference is not None and not isinstance(item, DataTag):
                quote = self.quote or item.text
                start = reference["start"] + item.text.find(quote)
                text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
                point = CheckPoint(
                    text=text,
                    reference=ReferenceCitation(
                        document_id=reference["id"],
                        version=reference["version"],
                        start=start,
                        end=start + len(quote),
                        quote=quote,
                    ),
                )
                draft = result.output.model_copy(update={"check_points": [point]})
                return type(result)(output=draft, model=result.model, usage=result.usage)
        return result


def _find(request, quote):
    """Locate `quote` in the entry blocks of a request, returning (entry_id, offset in entry)."""
    entry_id = None
    for item in request.inputs:
        if getattr(item, "name", None) == "entry" and not item.closing:
            entry_id = dict(item.attributes)["id"]
        elif hasattr(item, "spans") and entry_id is not None and quote in item.text:
            return str(entry_id), item.text.index(quote)
    raise AssertionError(f"{quote!r} not shown to the model")
