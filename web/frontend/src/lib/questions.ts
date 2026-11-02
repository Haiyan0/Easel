import type { ChatQuestionItem } from './api';

/** 题卡提交载荷：每个问题都带当前选择；local/control 共用同一视图。 */
export function questionAnswersFor(
  items: ChatQuestionItem[],
  selected: Record<string, string[]>,
): Record<string, string[]> {
  const answers: Record<string, string[]> = {};
  for (const item of items) {
    answers[item.questionId] = [...(selected[item.questionId] || [])];
  }
  return answers;
}
