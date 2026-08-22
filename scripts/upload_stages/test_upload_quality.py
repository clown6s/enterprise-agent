# -*- coding: utf-8 -*-
import json
from upload_test_common import parsed

if __name__ == '__main__':
    path,data,text,diagnostics=parsed()
    print(json.dumps({'stage':'quality','file':path,'diagnostics':diagnostics or {'action':'not_pdf'}},ensure_ascii=False,indent=2))
