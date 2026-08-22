# -*- coding: utf-8 -*-
"""真实上下文拼装诊断。"""
from query_test_common import get_args, write_result
from app.service import retrieval, agent

def main():
    args=get_args(); q=args.query
    try:
        hits=retrieval.retrieve(q,extra_queries=agent._expand_queries(q))
        ctx=retrieval.build_context(hits)
        result={'stage':'context','query':q,'hit_count':len(hits),'chars':len(ctx),
                'sources':[{'source':h.get('source'),'page':h.get('page'),'document_id':h.get('document_id')} for h in hits],
                'context':ctx,'truncated':len(ctx)>=6000}
    except Exception as exc:
        result={'stage':'context','query':q,'status':'error','error_type':type(exc).__name__,'error':str(exc)[:500]}
    write_result(q,'context',result,args.output)
if __name__=='__main__': main()
