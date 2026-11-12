"""Expose a labelled local-source draft while the main research continues."""

from ..errors import Stopped, ToolFailure
from ..source_contracts import SourceComposition
from .source_workflow import draft_of


async def write_preview(services, sources, understanding):
    key = "draft:preview:v1"
    if await services.db.saved_value(services.run_id, key) is not None:
        return
    selected = sources[:2]
    passages = [
        {
            "passage_id": f"P{index}",
            "source_id": source["source_id"],
            "text": source.get("identity_context", "")[:12000],
        }
        for index, source in enumerate(selected, 1)
        if source.get("identity_context")
    ]
    if not passages:
        return
    offered = {passage["passage_id"] for passage in passages}

    def validate(composition):
        for section in composition.sections:
            for paragraph in section.paragraphs:
                if not set(paragraph.passage_ids) <= offered:
                    raise ValueError("Use only offered local passage IDs")

    try:
        value = await services.llm.generate(
            "write:local-preview:v1",
            "writer",
            {
                "question": services.request.query,
                "output_language": services.request.output_language,
                "reader_goal": understanding.get("reader_goal"),
                "reader_questions": understanding.get("facets", []),
                "answer_stage": "Preliminary local-source explanation. The application labels it unverified while further research continues. Focus on the requested definition and mechanism; do not write a source-audit chapter.",
                "sources": [
                    {
                        "source_id": source["source_id"],
                        "title": source["title"],
                        "url": source["url"],
                        "identity_established": False,
                    }
                    for source in selected
                ],
                "passages": passages,
            },
            SourceComposition,
            partition="finalization",
            validator=validate,
        )
        await services.db.commit_value(
            services.run_id, services.fence, key, "unverified_draft", draft_of(value).model_dump(mode="json")
        )
    except (Stopped, ToolFailure) as error:
        if error.code != "cancelled":
            await services.warn("early_draft.unavailable", reason=error.code)
    except Exception as error:  # noqa: BLE001 - isolate this optional preview; cancellation propagates separately.
        await services.warn("early_draft.unavailable", reason=type(error).__name__)
