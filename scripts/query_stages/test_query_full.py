# -*- coding: utf-8 -*-
"""只读完整查询诊断；真实检索/模型，隔离生产写操作。"""
from query_test_common import get_args, write_result
from app.service import agent

def main():
    args=get_args(); q=args.query
    result={'stage':'full','query':q,'note':'run lower stages individually for exact evidence'}
    try:
        # 不调用 agent.answer，避免 history/audit/gap/review/synonym 副作用。
        from app.service import retrieval, verify
        hits=retrieval.retrieve(q,extra_queries=agent._expand_queries(q))
        ctx=retrieval.build_context(hits)
        raw=agent._llm_call(f"【资料】\n{ctx}\n\n【用户问题】\n{q}") if ctx.strip() else ''
        final=verify.verify(raw,ctx,query=q) if raw and ctx else raw
        result.update({'status':'ok','hits':len(hits),'sources':[h.get('source') for h in hits],
                       'context_chars':len(ctx),'raw_answer':raw,'final_answer':final,
                       'graph':'not_called','production_side_effects':False})
    except Exception as exc:
        result.update({'status':'error','error_type':type(exc).__name__,'error':str(exc)[:500]})
    write_result(q,'full',result,args.output)
if __name__=='__main__': main()
