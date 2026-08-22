# -*- coding: utf-8 -*-
import json
from upload_test_common import parsed, chunk_document

if __name__ == '__main__':
    path,data,text,diagnostics=parsed(); chunks=chunk_document(text,path,600)
    print(json.dumps({'stage':'graph_mock','file':path,'chunks':len(chunks),'real_neo4j':False,'input_sample':[x['text'][:120] for x in chunks[:3]]},ensure_ascii=False,indent=2))
