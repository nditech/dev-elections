# Create your views here.
from django.http import HttpResponse
from webapp.models import *
from models import *
from api import *
from queries import checklist_status
from django.contrib.auth.decorators import login_required, permission_required
from djangomako.shortcuts import render_to_string
import json
from xlwt import *
from datetime import datetime

@login_required
def dashboard_stats(request):
    province = request.GET.get('province', None)
    district = request.GET.get('district', None)
    polling_district = request.GET.get('polling_district', None)
    polling_stream = request.GET.get('polling_stream', None)
    sample = request.GET.get('sample', None)
    
    # checklist statistics
    checklist_filter = {}
    
    if polling_stream:
        try:
            checklist_filter['location__id'] = int(polling_stream)
        except ValueError:
            pass
    elif polling_district:
        try:
            checklist_filter['location__parent__parent__id'] = int(polling_district)
        except ValueError:
            pass
    elif district:
        try:
            checklist_filter['location__parent__parent__parent__parent__parent__id'] = int(district)
        except ValueError:
            pass
    elif province:
        try:
            checklist_filter['location__parent__parent__parent__parent__parent__parent__id'] = int(province)
        except ValueError:
            pass
    if sample:
        checklist_filter['location__pk__in'] = Sample.objects.filter(sample=sample).values_list('location__pk', flat=True)
    
    dashboard_checklist_status = {
        'setup_complete': Checklist.objects.filter(**checklist_filter).filter(**checklist_status['setup_complete']).count(),
        'setup_missing': Checklist.objects.filter(**checklist_filter).filter(**checklist_status['setup_missing']).count(),
        'setup_partial': Checklist.objects.filter(**checklist_filter).filter(checklist_status['setup_partial']).count(),
        
        'voting_complete': Checklist.objects.filter(**checklist_filter).filter(**checklist_status['voting_complete']).count(),
        'voting_missing': Checklist.objects.filter(**checklist_filter).filter(**checklist_status['voting_missing']).count(),
        'voting_partial': Checklist.objects.filter(**checklist_filter).filter(checklist_status['voting_partial']).count(),
        
        'closing_complete': Checklist.objects.filter(**checklist_filter).filter(**checklist_status['closing_complete']).count(),
        'closing_missing': Checklist.objects.filter(**checklist_filter).filter(**checklist_status['closing_missing']).count(),
        'closing_partial': Checklist.objects.filter(**checklist_filter).filter(checklist_status['closing_partial']).count(),
        
        'counting_complete': Checklist.objects.filter(**checklist_filter).filter(**checklist_status['counting_complete']).count(),
        'counting_missing': Checklist.objects.filter(**checklist_filter).filter(**checklist_status['counting_missing']).count(),
        'counting_partial': Checklist.objects.filter(**checklist_filter).filter(checklist_status['counting_partial']).count(),
    }
         
    return HttpResponse(json.dumps(dashboard_checklist_status), mimetype="application/json")

@login_required
@permission_required('webapp.can_export')
def export_checklists(request):
    request_params = request.GET
    resource = ChecklistsResource()
    
    applicable_filters = resource.build_filters(request_params)
    obj_list = list(resource.apply_filters(request, applicable_filters))
    
    wb = Workbook()
    ws = wb.add_sheet('Checklists');
    
    # write row header
    columns = ['PDID', 'Monitor Id', 'Time', 'Province', 'District', 'Constituency', 'Ward', 
        'Polling District', 'Polling Station', 'Polling Stream',
        '1', '2', '3a', '3b', '3c', '3d', '3e', '3f', '3g', '3h', '4', '5', '5a',
        '5b', '5c', '6', '7', '8', '9', '10', '11', '12', '13', '14', '15', '16',
        '17', '18', '19', '20', '21', '22', '23', '24', '25', '26', '27', 
        '28a1', '28a2', '28b1', '28b2', '28c1', '28c2', '28d1', '28d2', '28e1', '28e2',
        '29a1', '29a2', '29b1', '29b2', '29c1', '29c2', '29d1', '29d2', '29e1', '29e2',
        '29f1', '29f2', '29g1', '29g2', '29h1', '29h2', '29i1', '29i2', '29j1', '29j2',
        '29k1', '29k2', '29l1', '29l2', '30', '31', '32', '33']
    
    data_fields = ['location.parent.code', 'observer.observer_id', 'updated', 'location.parent.parent.parent.parent.parent.parent.name', 
        'location.parent.parent.parent.parent.parent.name', 'location.parent.parent.parent.parent.name', 
        'location.parent.parent.parent.name', 
        'location.parent.parent.name', 'location.parent.name', 'location.name',
        'response.A', 'response.B', 'response.CA', 'response.CB', 'response.CC', 'response.CD', 'response.CE', 
        'response.CF', 'response.CG', 'response.CH', 'response.D', 'response.E', 'response.EA',
        'response.EB', 'response.EC', 'response.F', 'response.G', 'response.H', 'response.J', 'response.K',
        'response.M', 'response.N', 'response.P', 'response.Q', 'response.R', 'response.S',
        'response.T', 'response.U', 'response.V', 'response.W', 'response.X', 'response.Y', 'response.Z', 
        'response.AA', 'response.AB', 'response.AC', 'response.AD', 
        'response.AEAA', 'response.AEAB', 'response.AEBA', 'response.AEBB', 'response.AECA', 'response.AECB', 
        'response.AEDA', 'response.AEDB', 'response.AEEA', 'response.AEEB',
        'response.AGA', 'response.AGB', 'response.AHA', 'response.AHB', 'response.AJA', 'response.AJB', 
        'response.AKA', 'response.AKB', 'response.AMA', 'response.AMB',
        'response.ANA', 'response.ANB', 'response.APA', 'response.APB', 'response.AQA', 'response.AQB', 
        'response.ARA', 'response.ARB', 'response.ASA', 'response.ASB',
        'response.ATA', 'response.ATB', 'response.AUA', 'response.AUB', 'response.AV', 'response.AW', 'response.AX',
        'response.AY']
    
    for i, column in enumerate(columns):
        ws.write(0, i, column)

    for row, checklist in enumerate(obj_list):
        for j, field in enumerate(data_fields):
            exec 'value = checklist.%s' % field
            ws.write(row+1, j, str(value))
    
    response = HttpResponse(mimetype='application/vnd.ms-excel')
    response['Content-Disposition'] = 'attachment; filename=checklist_export-%d%02d%02d-%02d%02d.xls' % \
        (datetime.today().year, datetime.today().month, datetime.today().day, datetime.today().hour, datetime.today().minute)

    wb.save(response)
    return response
        
def app_templates(context):
    return render_to_string('zambia/templates.html', {}, context)