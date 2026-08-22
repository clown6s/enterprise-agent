# Structure-aware chunking decision

先实现 `legal_article` 独立 profile 和无副作用预览，不改变生产 ingest。法规低置信回退 generic；预览确认后再接入生产。