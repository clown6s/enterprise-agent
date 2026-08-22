# -*- coding: utf-8 -*-
import json
from upload_test_common import parsed, classify_document, chunk_document

if __name__ == '__main__':
    path,data,text,diagnostics=parsed(); c=classify_document(text,path); chunks=chunk_document(text,path,600)
    print(json.dumps({'stage':'chunk','file':path,'profile':c.kind,'count':len(chunks),'max_chars':max((len(x['text']) for x in chunks),default=0),'chunks':chunks},ensure_ascii=False,indent=2))
