import {useRef, useState} from 'react';
import type {SourceEditorFormValue} from '@/components/source-editor-dialog';
import {
  activateSourcePreferenceProfileDraft,
  createSource,
  deleteSourcePreferenceProfileDraft,
  getSourceInsightRouting,
  getSourcePreferenceProfile,
  previewSource,
  saveSourcePreferenceProfileDraft,
  updateSource,
  updateSourceInsightRouting,
} from '@/services/source-service';
import type {FeedItem} from '@/types/feed';
import type {
  SourceDto,
  SourcePreferenceProfileInput,
  SourcePreferenceProfileState,
} from '@/types/source';
import {
  buildDefaultSourceRouting,
  buildSourceCreatePayload,
  normalizeSourceRoutingInput,
  sourcePreferenceProfileInputMatchesProfile,
  type SourceEditorState,
} from '@/hooks/info-flow/helpers';

interface UseSourceEditorControllerArgs {
  insightChannelCount: number;
  reloadInsightChannels: () => Promise<unknown>;
  reloadSources: () => Promise<SourceDto[]>;
  reloadHomepageItems: () => Promise<FeedItem[]>;
  setSourceSyncMessage: (message: string | null) => void;
  shouldReloadHomepageAfterSourceSave: () => boolean;
  startAutoCreateSourceSync: (source: SourceDto, sourceName: string) => Promise<void>;
}

export function useSourceEditorController({
  insightChannelCount,
  reloadInsightChannels,
  reloadSources,
  reloadHomepageItems,
  setSourceSyncMessage,
  shouldReloadHomepageAfterSourceSave,
  startAutoCreateSourceSync,
}: UseSourceEditorControllerArgs) {
  const [sourceEditor, setSourceEditor] = useState<SourceEditorState | null>(null);
  const [isPreviewingSource, setIsPreviewingSource] = useState(false);
  const [isSavingSource, setIsSavingSource] = useState(false);
  const pendingCreatedSourceRef = useRef<SourceDto | null>(null);
  const pendingCreatedSourceProfileRef = useRef<SourcePreferenceProfileState | null>(null);

  async function previewQuickSource(url: string) {
    if (isPreviewingSource) {
      return;
    }

    setIsPreviewingSource(true);
    try {
      const preview = await previewSource({url});
      if (insightChannelCount === 0) {
        void reloadInsightChannels();
      }
      pendingCreatedSourceRef.current = null;
      pendingCreatedSourceProfileRef.current = null;
      setSourceEditor({
        mode: 'create',
        source: null,
        preview,
        routing: buildDefaultSourceRouting(0),
        routingError: null,
        isRoutingLoading: false,
        sourcePreferenceProfile: null,
        sourceProfileError: null,
        isSourceProfileLoading: false,
      });
    } finally {
      setIsPreviewingSource(false);
    }
  }

  function openSourceEditor(source: SourceDto) {
    pendingCreatedSourceRef.current = null;
    pendingCreatedSourceProfileRef.current = null;
    setSourceEditor({
      mode: 'edit',
      source,
      preview: null,
      routing: null,
      routingError: null,
      isRoutingLoading: true,
      sourcePreferenceProfile: null,
      sourceProfileError: null,
      isSourceProfileLoading: true,
    });
    if (insightChannelCount === 0) {
      void reloadInsightChannels();
    }

    void getSourceInsightRouting(source.id)
      .then((routing) => {
        setSourceEditor((current) =>
          current?.mode === 'edit' && current.source?.id === source.id
            ? {...current, routing, routingError: null, isRoutingLoading: false}
            : current,
        );
      })
      .catch((error) => {
        console.error('Failed to load source insight routing', error);
        const message = error instanceof Error ? error.message : 'Source insight routing failed to load.';
        setSourceEditor((current) =>
          current?.mode === 'edit' && current.source?.id === source.id
            ? {...current, routing: null, routingError: message, isRoutingLoading: false}
            : current,
        );
      });

    void getSourcePreferenceProfile(source.id)
      .then((profileState) => {
        setSourceEditor((current) =>
          current?.mode === 'edit' && current.source?.id === source.id
            ? {...current, sourcePreferenceProfile: profileState, sourceProfileError: null, isSourceProfileLoading: false}
            : current,
        );
      })
      .catch((error) => {
        console.error('Failed to load source preference profile', error);
        const message = error instanceof Error ? error.message : 'Source Preference Profile failed to load.';
        setSourceEditor((current) =>
          current?.mode === 'edit' && current.source?.id === source.id
            ? {...current, sourcePreferenceProfile: null, sourceProfileError: message, isSourceProfileLoading: false}
            : current,
        );
      });
  }

  function closeSourceEditor() {
    if (!isSavingSource) {
      pendingCreatedSourceRef.current = null;
      pendingCreatedSourceProfileRef.current = null;
      setSourceEditor(null);
    }
  }

  async function saveSourceEditorProfileDraft(value: SourcePreferenceProfileInput) {
    if (!sourceEditor?.source) {
      return;
    }
    const profileState = await saveSourcePreferenceProfileDraft(sourceEditor.source.id, value);
    setSourceEditor((current) =>
      current?.mode === 'edit' && current.source?.id === sourceEditor.source?.id
        ? {...current, sourcePreferenceProfile: profileState, sourceProfileError: null, isSourceProfileLoading: false}
        : current,
    );
  }

  async function deleteSourceEditorProfileDraft() {
    if (!sourceEditor?.source) {
      return;
    }
    const profileState = await deleteSourcePreferenceProfileDraft(sourceEditor.source.id);
    setSourceEditor((current) =>
      current?.mode === 'edit' && current.source?.id === sourceEditor.source?.id
        ? {...current, sourcePreferenceProfile: profileState, sourceProfileError: null, isSourceProfileLoading: false}
        : current,
    );
  }

  async function saveAndActivateSourceProfile(sourceId: number, value: SourcePreferenceProfileInput) {
    const currentProfileState =
      pendingCreatedSourceRef.current?.id === sourceId
        ? pendingCreatedSourceProfileRef.current
        : sourceEditor?.mode === 'edit' && sourceEditor.source?.id === sourceId
          ? sourceEditor.sourcePreferenceProfile
          : null;
    if (
      currentProfileState?.active &&
      !currentProfileState.draft &&
      sourcePreferenceProfileInputMatchesProfile(value, currentProfileState.active)
    ) {
      return currentProfileState;
    }
    const draftState = await saveSourcePreferenceProfileDraft(sourceId, value);
    const activeState = await activateSourcePreferenceProfileDraft(sourceId, value.editNote ?? null);
    if (pendingCreatedSourceRef.current?.id === sourceId) {
      pendingCreatedSourceProfileRef.current = activeState;
    }
    setSourceEditor((current) =>
      current?.mode === 'edit' && current.source?.id === sourceId
        ? {...current, sourcePreferenceProfile: activeState, sourceProfileError: null, isSourceProfileLoading: false}
        : current,
    );
    return activeState ?? draftState;
  }

  async function saveSourceEditor(value: SourceEditorFormValue) {
    if (!sourceEditor || isSavingSource) {
      return;
    }

    setIsSavingSource(true);
    try {
      if (
        sourceEditor.mode === 'edit' &&
        value.isInsightRoutingDirty &&
        sourceEditor.routingError &&
        sourceEditor.routing === null
      ) {
        throw new Error('Insight routing 未成功加载，不能从默认空 routing 修改后保存。');
      }
      const routingInput = value.isInsightRoutingDirty
        ? normalizeSourceRoutingInput(value.insightRouting)
        : null;
      if (sourceEditor.mode === 'edit' && sourceEditor.source) {
        if (value.isSourcePreferenceProfileDirty || sourceEditor.sourcePreferenceProfile?.draft) {
          await saveAndActivateSourceProfile(sourceEditor.source.id, value.sourcePreferenceProfile);
        }
        await updateSource(sourceEditor.source.id, {
          name: value.name,
          url: value.url,
          site_url: value.site_url,
          folder_path: value.folder_path,
          is_active: value.is_active,
          ai_route: value.ai_route,
        });
        if (routingInput) {
          try {
            await updateSourceInsightRouting(sourceEditor.source.id, routingInput);
          } catch (routingError) {
            const message = routingError instanceof Error ? routingError.message : 'Source insight routing save failed.';
            setSourceEditor((current) =>
              current?.mode === 'edit' && current.source?.id === sourceEditor.source?.id
                ? {...current, routingError: `Insight routing 保存失败：${message}`, isRoutingLoading: false}
                : current,
            );
            throw new Error(`Insight routing 保存失败：${message}`);
          }
        }
        setSourceSyncMessage(`已更新订阅：${value.name}`);
      } else {
        let created = pendingCreatedSourceRef.current;
        if (created && created.url === value.url) {
          created = await updateSource(created.id, {
            name: value.name,
            url: value.url,
            site_url: value.site_url,
            folder_path: value.folder_path,
            is_active: value.is_active,
            ai_route: value.ai_route,
          });
        } else {
          created = await createSource(buildSourceCreatePayload(value));
          pendingCreatedSourceProfileRef.current = null;
        }
        pendingCreatedSourceRef.current = created;
        if (value.isSourcePreferenceProfileDirty) {
          await saveAndActivateSourceProfile(created.id, value.sourcePreferenceProfile);
        }
        if (routingInput) {
          try {
            await updateSourceInsightRouting(created.id, routingInput);
          } catch (routingError) {
            const message = routingError instanceof Error ? routingError.message : 'Source insight routing save failed.';
            setSourceEditor((current) =>
              current?.mode === 'create'
                ? {...current, routingError: `Insight routing 保存失败：${message}`, isRoutingLoading: false}
                : current,
            );
            throw new Error(`Insight routing 保存失败：${message}`);
          }
        }
        setSourceSyncMessage(`已添加订阅：${value.name}，正在刷新新来源。`);
        await startAutoCreateSourceSync(created, value.name);
        pendingCreatedSourceRef.current = null;
        pendingCreatedSourceProfileRef.current = null;
      }

      await reloadSources();
      setSourceEditor(null);
      if (shouldReloadHomepageAfterSourceSave()) {
        void reloadHomepageItems().catch((error) => console.error('Failed to reload homepage after source save', error));
      }
    } catch (error) {
      console.error('Failed to save source editor value', error);
      setSourceSyncMessage(error instanceof Error ? `订阅保存失败：${error.message}` : '订阅保存失败。');
      throw error;
    } finally {
      setIsSavingSource(false);
    }
  }

  return {
    sourceEditor,
    isPreviewingSource,
    isSavingSource,
    closeSourceEditor,
    deleteSourceEditorProfileDraft,
    openSourceEditor,
    previewQuickSource,
    saveSourceEditor,
    saveSourceEditorProfileDraft,
  };
}
