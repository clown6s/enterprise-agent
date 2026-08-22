# -*- coding: utf-8 -*-
import json
from upload_test_common import parsed, classify_document

if __name__ == '__main__':
    path,data,text,diagnostics=parsed(); c=classify_document(text,path)
    print(json.dumps({'stage':'structure','file':path,'profile':c.kind,'confidence':c.confidence,'evidence':c.evidence},ensure_ascii=False,indent=2))
