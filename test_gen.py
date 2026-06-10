import sys, traceback
sys.path.insert(0, r'C:\Users\haliza.LUXOR\.claude\skills\static-remix')

try:
    from ui_server import build_prompt, load_brand
    brand = load_brand()
    brand['_active_product_name'] = 'Jeli Gamat 550ML'
    p = build_prompt('','','Joint Pain','test ctx',True,True,brand,
                     copy_h1='H1',copy_h2='H2',copy_cta='CTA',copy_data={})
    print('build_prompt OK:', len(p), 'chars')
    print(p[:300])
except Exception as e:
    traceback.print_exc()
