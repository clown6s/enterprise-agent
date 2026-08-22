# -*- coding: utf-8 -*-
import json
from upload_test_common import parsed, classify_document, chunk_document

if __name__ == '__main__':
    path,data,text,diagnostics=parsed(); c=classify_document(text,path); chunks=chunk_document(text,path,600)
    result={'stage':'full_dry_run','file':path,'profile':c.kind,'confidence':c.confidence,'chunks':len(chunks),'max_chars':max((len(x['text']) for x in chunks),default=0),'embedding':'mock','milvus':'captured','neo4j':'mock','side_effects':False,'real_external_calls':False}
    print(json.dumps(result,ensure_ascii=False,indent=2))
