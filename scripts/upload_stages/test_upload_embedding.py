# -*- coding: utf-8 -*-
import json
from upload_test_common import parsed, chunk_document

if __name__ == '__main__':
    path,data,text,diagnostics=parsed(); chunks=chunk_document(text,path,600)
    vectors=[[0.0]*8 for _ in chunks]
    print(json.dumps({'stage':'embedding_mock','file':path,'chunks':len(chunks),'dimension':8,'real_api':False},ensure_ascii=False,indent=2))
