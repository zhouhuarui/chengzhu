# Locked role model profiles

The competition package uses two explicit Alibaba Cloud model profiles rather
than one model for every role:

| Roles | Model | Reasoning policy |
|---|---|---|
| Research Lead, both collectors, Report Writer, Compliance Reviewer | `qwen3-30b-a3b-instruct-2507` | Non-thinking instruction model for bounded planning, retrieval handoffs, writing, and review |
| Quality Analyst, Growth Analyst, Evidence Judge | `qwen3.5-plus` | Strong hybrid-reasoning model for analysis, cross-challenge, and adjudication |

The official visual Skill independently pins its supplied script and its
default `qwen3.5-plus` model. Model IDs are validated by
`scripts/verify.sh`; changing one requires a package rebuild and a new runtime
evidence capture.

Alibaba Cloud's model catalog records that `qwen3.5-plus` supports thinking
mode and that `qwen3-30b-a3b-instruct-2507` is a non-thinking instruction
model with function calling:
<https://help.aliyun.com/zh/model-studio/text-generation-model/>.
