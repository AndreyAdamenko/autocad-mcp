"""Read viewport metadata from already-open staged facade DWGs, without activation."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from autocad_mcp.acad_core import com_apartment,running_app
from autocad_mcp.com_read import method,read,maybe

with com_apartment():
    app=running_app()
    docs=read(app,'Documents')
    for i in range(int(read(docs,'Count'))):
        doc=method(docs,'Item',i)
        path=str(read(doc,'FullName'))
        if 'spz_sources' not in path or '8_9' not in path:
            continue
        rows=[]
        layouts=read(doc,'Layouts')
        for j in range(int(read(layouts,'Count'))):
            layout=method(layouts,'Item',j)
            if str(read(layout,'Name'))=='Model':continue
            block=read(layout,'Block')
            for k in range(int(read(block,'Count'))):
                obj=method(block,'Item',k)
                if str(read(obj,'ObjectName'))=='AcDbViewport':
                    rows.append({'layout':str(read(layout,'Name')),'id':str(read(obj,'Handle')),
                                 **{key:maybe(obj,key) for key in ('Target','Direction','Center','Width','Height','CustomScale','TwistAngle','ViewportOn')}})
        print(json.dumps({'file':Path(path).name,'viewports':rows},ensure_ascii=False,default=list),flush=True)
