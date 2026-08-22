# -*- coding: utf-8 -*-
import json
from upload_test_common import parsed

if __name__ == '__main__':
    path,data,text,diagnostics=parsed()
    print(json.dumps({'stage':'parse','file':path,'bytes':len(data),'chars':len(text),'diagnostics':diagnostics},ensure_ascii=False,indent=2))
