export const GLOBAL_INSIGHT_ARTIFACT_FILL_LIMIT = 200;

export type InsightArtifactFillScope = 'current_source' | 'global';

export interface InsightArtifactFillPlan {
  requestLimit: number;
  operationLimit: number | null;
  queuedCount: number;
  shouldStartOperation: boolean;
  message: string;
}

export function buildInsightArtifactFillPlan(
  scope: InsightArtifactFillScope,
  totalMissingCount: number,
): InsightArtifactFillPlan {
  const requestLimit = GLOBAL_INSIGHT_ARTIFACT_FILL_LIMIT;
  const operationLimit = scope === 'global' ? GLOBAL_INSIGHT_ARTIFACT_FILL_LIMIT : null;
  const queuedCount = operationLimit === null ? totalMissingCount : Math.min(totalMissingCount, operationLimit);
  if (totalMissingCount === 0) {
    return {
      requestLimit,
      operationLimit,
      queuedCount: 0,
      shouldStartOperation: false,
      message: scope === 'global' ? '当前没有需要补齐的全局未成功洞察。' : '当前来源没有需要补齐的未成功洞察。',
    };
  }
  if (scope === 'global' && totalMissingCount > requestLimit) {
    return {
      requestLimit,
      operationLimit,
      queuedCount,
      shouldStartOperation: true,
      message: `当前共有 ${totalMissingCount} 条未成功洞察，本轮最多补齐 ${requestLimit} 条，已排队 ${queuedCount} 条；剩余可在本轮完成后再次补齐。`,
    };
  }
  return {
    requestLimit,
    operationLimit,
    queuedCount,
    shouldStartOperation: true,
    message:
      scope === 'global'
        ? `当前共有 ${totalMissingCount} 条未成功洞察，已排队补齐 ${queuedCount} 条。`
        : `当前来源共有 ${totalMissingCount} 条未成功洞察，已排队补齐 ${queuedCount} 条。`,
  };
}
