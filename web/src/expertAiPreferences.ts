import type { ExpertAiModel } from "./expertTypes.ts";

export interface ExpertAiPreferences {
  model: string;
  reasoning_effort: string;
}

export function resolveAiPreferences(
  models: ExpertAiModel[],
  preferred: Partial<ExpertAiPreferences>,
): ExpertAiPreferences {
  const model = models.find((item) => item.model === preferred.model)
    ?? models.find((item) => item.is_default)
    ?? models[0];
  if (!model) return { model: "", reasoning_effort: "" };
  return {
    model: model.model,
    reasoning_effort: model.reasoning_efforts.includes(preferred.reasoning_effort ?? "")
      ? preferred.reasoning_effort!
      : model.default_reasoning_effort,
  };
}

export function reasoningEffortLabel(effort: string): string {
  const labels: Record<string, string> = {
    none: "无推理", minimal: "最轻", low: "低", medium: "中",
    high: "高", xhigh: "很高", max: "最高", ultra: "超高",
  };
  return labels[effort] ?? effort;
}
