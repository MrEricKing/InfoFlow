import {GLOBAL_INSIGHT_ARTIFACT_FILL_LIMIT, buildInsightArtifactFillPlan} from './insight-artifact-fill.js';

function assertEqual(actual: unknown, expected: unknown, message: string) {
  if (actual !== expected) {
    throw new Error(`${message}: expected ${String(expected)}, got ${String(actual)}`);
  }
}

const emptyGlobal = buildInsightArtifactFillPlan('global', 0);
assertEqual(emptyGlobal.shouldStartOperation, false, 'global empty should not start');
assertEqual(emptyGlobal.operationLimit, GLOBAL_INSIGHT_ARTIFACT_FILL_LIMIT, 'global operation limit');
assertEqual(emptyGlobal.queuedCount, 0, 'global empty queued count');
assertEqual(emptyGlobal.message, '当前没有需要补齐的全局未成功洞察。', 'global empty message');

const largeGlobal = buildInsightArtifactFillPlan('global', 2100);
assertEqual(largeGlobal.shouldStartOperation, true, 'large global should start');
assertEqual(largeGlobal.operationLimit, GLOBAL_INSIGHT_ARTIFACT_FILL_LIMIT, 'large global operation limit');
assertEqual(largeGlobal.queuedCount, GLOBAL_INSIGHT_ARTIFACT_FILL_LIMIT, 'large global queued count');
assertEqual(
  largeGlobal.message,
  '当前共有 2100 条未成功洞察，本轮最多补齐 200 条，已排队 200 条；剩余可在本轮完成后再次补齐。',
  'large global message',
);

const sourcePlan = buildInsightArtifactFillPlan('current_source', 317);
assertEqual(sourcePlan.shouldStartOperation, true, 'source should start');
assertEqual(sourcePlan.operationLimit, null, 'source operation should not use global cap');
assertEqual(sourcePlan.queuedCount, 317, 'source queued count');
assertEqual(sourcePlan.requestLimit, GLOBAL_INSIGHT_ARTIFACT_FILL_LIMIT, 'source preflight request limit');
assertEqual(sourcePlan.message, '当前来源共有 317 条未成功洞察，已排队补齐 317 条。', 'source message');
