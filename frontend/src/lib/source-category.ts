export const EMPTY_SOURCE_CATEGORY_ID = '__empty__';
export const EMPTY_SOURCE_CATEGORY_LABEL = '未分类';

export function sourceCategoryIdFromFolderPath(value: string | null | undefined) {
  const normalized = value?.trim() ?? '';
  return normalized || EMPTY_SOURCE_CATEGORY_ID;
}

export function sourceCategoryLabelFromFolderPath(value: string | null | undefined) {
  const normalized = value?.trim() ?? '';
  return normalized || EMPTY_SOURCE_CATEGORY_LABEL;
}

export function sourceCategoryLabelFromId(categoryId: string) {
  return categoryId === EMPTY_SOURCE_CATEGORY_ID ? EMPTY_SOURCE_CATEGORY_LABEL : categoryId;
}
