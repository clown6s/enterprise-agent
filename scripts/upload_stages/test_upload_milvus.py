# -*- coding: utf-8 -*-
import json
from upload_test_common import parsed, chunk_document

if __name__ == '__main__':
    path,data,text,diagnostics=parsed(); chunks=chunk_document(text,path,600)
    rows=[{'text':x['text'],'page':x.get('page',1),'document_id':'DRY-RUN','source':path} for x in chunks]
    print(json.dumps({'stage':'milvus_mock','file':path,'rows':len(rows),'real_milvus':False,'sample':rows[:3]},ensure_ascii=False,indent=2))
