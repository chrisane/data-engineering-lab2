from pathlib import Path
import random
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
from faker import Faker

SEED = 20261004
random.seed(SEED)
np.random.seed(SEED)
fake = Faker("en_US")
Faker.seed(SEED)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "incoming"
OUT.mkdir(parents=True, exist_ok=True)

START = pd.Timestamp("2026-01-01")
END = pd.Timestamp("2026-12-31")
VAT = 0.15

def money(x):
    return round(float(x), 2)

def rand_date(start=START, end=END):
    days = (end-start).days
    return start + pd.Timedelta(days=random.randint(0, days))

# ---------- ORGANISATION ----------
sites = [
    ("HO001","Head Office","Windhoek","HEAD_OFFICE"),
    ("DC001","Central Distribution Centre","Windhoek","DISTRIBUTION_CENTRE"),
]
towns = ["Windhoek Central","Katutura","Swakopmund","Walvis Bay","Oshakati","Ondangwa",
         "Rundu","Otjiwarongo","Keetmanshoop","Mariental","Gobabis","Omaruru"]
for i,t in enumerate(towns,1):
    sites.append((f"BR{i:03d}",f"{t} Branch",t,"BRANCH"))
sites_df = pd.DataFrame(sites, columns=["site_id","site_name","town","site_type"])

departments = pd.DataFrame([
    ("D001","Finance"),("D002","Procurement"),("D003","Human Resources"),
    ("D004","Information Technology"),("D005","Operations"),("D006","Sales"),
    ("D007","Warehousing"),("D008","Security"),("D009","Marketing")
], columns=["department_id","department_name"])

# ---------- PRODUCTS ----------
categories = {
    "Grocery":["Maize Meal","Rice","Pasta","Cooking Oil","Sugar","Flour","Canned Beans","Cereal"],
    "Beverages":["Still Water","Fruit Juice","Soft Drink","Coffee","Tea"],
    "Household":["Dishwashing Liquid","Laundry Powder","Bleach","Paper Towels","Bin Bags"],
    "Personal Care":["Soap","Toothpaste","Shampoo","Body Lotion","Deodorant"],
    "Stationery":["A4 Paper","Pens","Notebooks","Files","Printer Cartridges"],
    "Specialized Equipment":["POS Scanner","Barcode Printer","Cash Drawer","UPS Unit"],
    "Local Specialty":["Local Craft Pack","Regional Food Pack","Gift Hamper"]
}
products=[]
pid=1
for cat,names in categories.items():
    for base in names:
        for variant in range(1,4):
            cost=money(random.uniform(8,3500) if cat=="Specialized Equipment" else random.uniform(5,400))
            margin=random.uniform(.18,.42)
            price=money(cost/(1-margin))
            products.append((f"P{pid:05d}",f"{base} {variant}",cat,cost,price,
                             cat not in ["Specialized Equipment","Local Specialty"]))
            pid+=1
products_df=pd.DataFrame(products,columns=["product_id","product_name","category","standard_cost","selling_price","dc_stocked"])

# ---------- SUPPLIERS ----------
suppliers=[]
for i in range(1,31):
    min_order=random.choice([5000,7500,10000,15000,20000,25000,50000])
    rebate=random.choice([0,.01,.015,.02,.025,.035])
    suppliers.append((f"SUP{i:04d}",fake.company(),fake.city(),min_order,rebate,random.choice([15,30,45,60])))
suppliers_df=pd.DataFrame(suppliers,columns=["supplier_id","supplier_name","location","minimum_order_value_nad","rebate_rate","payment_terms_days"])

product_suppliers=[]
for p in products_df.itertuples():
    choices=random.sample(list(suppliers_df.supplier_id),k=random.randint(1,3))
    for s in choices:
        product_suppliers.append((p.product_id,s, money(p.standard_cost*random.uniform(.92,1.08))))
product_suppliers_df=pd.DataFrame(product_suppliers,columns=["product_id","supplier_id","supplier_unit_cost"])

# ---------- CUSTOMERS ----------
customers=[]
for i in range(1,401):
    typ=random.choices(["RETAIL","BUSINESS"],weights=[.85,.15])[0]
    customers.append((f"CUST{i:05d}",fake.name() if typ=="RETAIL" else fake.company(),typ,
                      random.choice(towns), random.choice([0,0,0,5000,10000,25000]) if typ=="BUSINESS" else 0))
customers_df=pd.DataFrame(customers,columns=["customer_id","customer_name","customer_type","town","credit_limit"])

# ---------- PURCHASE ORDERS ----------
po_headers=[]; po_lines=[]; po_no=1
for month in range(1,13):
    for supplier in suppliers_df.itertuples():
        if random.random()<.70:
            po_id=f"PO{po_no:06d}"; po_no+=1
            d=pd.Timestamp(2026,month,random.randint(1,25))
            eligible=product_suppliers_df[product_suppliers_df.supplier_id==supplier.supplier_id]
            if eligible.empty: continue
            picks=eligible.sample(n=min(random.randint(2,8),len(eligible)),replace=False)
            lines=[]; subtotal=0
            for _,r in picks.iterrows():
                qty=random.randint(10,120)
                val=money(qty*r.supplier_unit_cost)
                lines.append((po_id,r.product_id,qty,r.supplier_unit_cost,val))
                subtotal+=val
            if subtotal<supplier.minimum_order_value_nad:
                # increase first line so monetary MOQ is satisfied
                first=list(lines[0])
                extra=np.ceil((supplier.minimum_order_value_nad-subtotal)/first[3])
                first[2]+=int(extra); first[4]=money(first[2]*first[3]); lines[0]=tuple(first)
                subtotal=sum(x[4] for x in lines)
            po_headers.append((po_id,supplier.supplier_id,d.date(),"DC001",money(subtotal),"APPROVED"))
            po_lines.extend(lines)
po_df=pd.DataFrame(po_headers,columns=["po_id","supplier_id","po_date","delivery_site_id","po_value","status"])
po_lines_df=pd.DataFrame(po_lines,columns=["po_id","product_id","quantity","unit_cost","line_value"])

# ---------- GOODS RECEIPTS / AP INVOICES ----------
grn=[]; invoices=[]
for po in po_df.itertuples():
    grn_id="GRN"+po.po_id[2:]
    inv_id="INV"+po.po_id[2:]
    grn_date=pd.Timestamp(po.po_date)+pd.Timedelta(days=random.randint(2,14))
    grn.append((grn_id,po.po_id,po.supplier_id,grn_date.date(),po.delivery_site_id))
    net=po.po_value; vat=money(net*VAT); total=money(net+vat)
    invoices.append((inv_id,po.po_id,po.supplier_id,(grn_date+pd.Timedelta(days=random.randint(0,5))).date(),net,vat,total,"OPEN"))
grn_df=pd.DataFrame(grn,columns=["grn_id","po_id","supplier_id","receipt_date","site_id"])
invoice_df=pd.DataFrame(invoices,columns=["invoice_id","po_id","supplier_id","invoice_date","net_amount","vat_amount","total_amount","status"])

# ---------- SALES ----------
sales=[]; sale_lines=[]; sale_no=1
branches=sites_df[sites_df.site_type=="BRANCH"].site_id.tolist()
for day in pd.date_range(START,END):
    for branch in branches:
        n=random.randint(2,7)
        for _ in range(n):
            sid=f"S{sale_no:08d}"; sale_no+=1
            cust=random.choice(customers_df.customer_id.tolist())
            channel=random.choices(["CASH","CARD","CREDIT"],[.25,.65,.10])[0]
            chosen=products_df.sample(random.randint(1,5))
            net=0
            for p in chosen.itertuples():
                qty=random.randint(1,6)
                line=money(qty*p.selling_price)
                net+=line
                sale_lines.append((sid,p.product_id,qty,p.selling_price,line,p.standard_cost))
            vat=money(net*VAT); total=money(net+vat)
            sales.append((sid,day.date(),branch,cust,channel,money(net),vat,total))
sales_df=pd.DataFrame(sales,columns=["sale_id","sale_date","site_id","customer_id","payment_method","net_amount","vat_amount","total_amount"])
sale_lines_df=pd.DataFrame(sale_lines,columns=["sale_id","product_id","quantity","unit_price","line_net","unit_cost"])

# ---------- EXPENSES ----------
expenses=[]; exp_no=1
expense_map={"RENT":"Rent","UTILITY":"Electricity and Water","CONNECTIVITY":"Internet and Connectivity",
             "FUEL":"Fuel","SECURITY":"Security","MAINTENANCE":"Repairs and Maintenance"}
for month in range(1,13):
    for site in sites_df.site_id:
        for typ,label in expense_map.items():
            if typ=="RENT" and site=="HO001": base=random.uniform(90000,150000)
            elif typ=="RENT": base=random.uniform(18000,55000)
            elif typ=="UTILITY": base=random.uniform(4000,30000)
            elif typ=="CONNECTIVITY": base=random.uniform(2500,12000)
            else: base=random.uniform(1500,18000)
            d=pd.Timestamp(2026,month,min(random.randint(1,25),25))
            expenses.append((f"EXP{exp_no:06d}",d.date(),site,typ,label,money(base)))
            exp_no+=1
expenses_df=pd.DataFrame(expenses,columns=["expense_id","expense_date","site_id","expense_type","description","amount"])

# ---------- FIXED ASSETS ----------
asset_classes={"Vehicles":5,"Computer Hardware":3,"Servers and Network Equipment":4,
               "Machinery and Equipment":5,"Furniture and Fittings":6}
assets=[]; aid=1
for site in sites_df.site_id:
    count=random.randint(4,12) if site.startswith("BR") else random.randint(15,30)
    for _ in range(count):
        cls=random.choice(list(asset_classes))
        cost=money(random.uniform(10000,900000))
        acq=rand_date(pd.Timestamp("2021-01-01"),pd.Timestamp("2026-09-30"))
        life=asset_classes[cls]
        monthly=money(cost/(life*12))
        months=max(0,min(life*12,(pd.Timestamp("2026-12-31").year-acq.year)*12+pd.Timestamp("2026-12-31").month-acq.month))
        accum=money(min(cost,monthly*months))
        assets.append((f"AST{aid:06d}",site,cls,acq.date(),cost,life,monthly,accum,money(cost-accum),"ACTIVE"))
        aid+=1
assets_df=pd.DataFrame(assets,columns=["asset_id","site_id","asset_class","acquisition_date","acquisition_cost","useful_life_years","monthly_depreciation","accumulated_depreciation","net_book_value","status"])

# ---------- LOCAL ASSET REGISTERS ----------
local_assets=[]
lid=1
for site in branches:
    for _ in range(random.randint(15,35)):
        desc=random.choice(["Chair","Table","Bookcase","CCTV Camera","Monitor","Keyboard","Small UPS","Network Switch"])
        local_assets.append((f"LCL{lid:06d}",site,random.choice(departments.department_id.tolist()),desc,
                             random.choice(["Good","Fair","Needs Repair"]),fake.name(),rand_date(pd.Timestamp("2022-01-01"),END).date()))
        lid+=1
local_assets_df=pd.DataFrame(local_assets,columns=["local_asset_id","site_id","department_id","description","condition","assigned_to","recorded_date"])

# ---------- REBATES ----------
rebates=[]
for supplier in suppliers_df.itertuples():
    for q in range(1,5):
        months=[(q-1)*3+1,(q-1)*3+2,(q-1)*3+3]
        pos=po_df[(pd.to_datetime(po_df.po_date).dt.month.isin(months)) & (po_df.supplier_id==supplier.supplier_id)]
        spend=money(pos.po_value.sum())
        rate=.035 if spend>=1_000_000 else .02 if spend>=500_000 else .01 if spend>=250_000 else 0
        rebates.append((supplier.supplier_id,f"2026-Q{q}",spend,rate,money(spend*rate)))
rebates_df=pd.DataFrame(rebates,columns=["supplier_id","period","qualifying_spend","rebate_rate","rebate_amount"])

# ---------- INVENTORY MOVEMENTS ----------
moves=[]; mid=1
for line in po_lines_df.itertuples():
    po=po_df[po_df.po_id==line.po_id].iloc[0]
    moves.append((f"MOV{mid:08d}",po.po_date,"DC001",line.product_id,"PURCHASE_RECEIPT",line.quantity,line.unit_cost,line.po_id)); mid+=1
sale_lookup = sales_df.set_index("sale_id")[["sale_date","site_id"]].to_dict("index")
for line in sale_lines_df.itertuples():
    sale=sale_lookup[line.sale_id]
    moves.append((f"MOV{mid:08d}",sale["sale_date"],sale["site_id"],line.product_id,"SALE",-line.quantity,line.unit_cost,line.sale_id)); mid+=1
inventory_movements_df=pd.DataFrame(moves,columns=["movement_id","movement_date","site_id","product_id","movement_type","quantity","unit_cost","source_document_id"])

# ---------- GL JOURNALS ----------
journals=[]; jid=1
def post(date, source, typ, debit, credit, amount, site):
    global jid
    if amount == 0: return
    j=f"JRN{jid:09d}"; jid+=1
    date=pd.Timestamp(date).date()  # always a real date, never text
    journals.extend([
        (j,date,source,typ,site,debit,money(amount),0.0),
        (j,date,source,typ,site,credit,0.0,money(amount))
    ])

for s in sales_df.itertuples():
    cashacct="1000" if s.payment_method=="CASH" else "1010" if s.payment_method=="CARD" else "1100"
    post(s.sale_date,s.sale_id,f"{s.payment_method}_SALE",cashacct,"4000",s.net_amount,s.site_id)
    post(s.sale_date,s.sale_id,f"{s.payment_method}_SALE_VAT",cashacct,"2030",s.vat_amount,s.site_id)
for sid,g in sale_lines_df.groupby("sale_id"):
    sale=sale_lookup[sid]
    cogs=money((g.quantity*g.unit_cost).sum())
    post(sale["sale_date"],sid,"COGS","5000","1200",cogs,sale["site_id"])
for inv in invoice_df.itertuples():
    post(inv.invoice_date,inv.invoice_id,"PURCHASE_INVOICE","1200","2000",inv.net_amount,"DC001")
    post(inv.invoice_date,inv.invoice_id,"PURCHASE_VAT","1400","2000",inv.vat_amount,"DC001")
expense_accounts={"RENT":"6100","UTILITY":"6110","CONNECTIVITY":"6120","FUEL":"6130","SECURITY":"6160","MAINTENANCE":"6150"}
for e in expenses_df.itertuples():
    post(e.expense_date,e.expense_id,e.expense_type,expense_accounts[e.expense_type],"1010",e.amount,e.site_id)
for r in rebates_df.itertuples():
    if r.rebate_amount:
        qend={"2026-Q1":"2026-03-31","2026-Q2":"2026-06-30","2026-Q3":"2026-09-30","2026-Q4":"2026-12-31"}[r.period]
        post(qend,f"REB-{r.supplier_id}-{r.period}","REBATE_ACCRUAL","2050","4200",r.rebate_amount,"HO001")
for a in assets_df.itertuples():
    # 2026 depreciation only for months asset is available
    acq=pd.Timestamp(a.acquisition_date)
    start=max(acq,START)
    months=max(0,13-start.month) if start.year==2026 else 12
    dep=money(min(a.acquisition_cost,a.monthly_depreciation*months))
    post("2026-12-31",a.asset_id,"DEPRECIATION","6300","1590",dep,a.site_id)

gl_df=pd.DataFrame(journals,columns=["journal_id","posting_date","source_document_id","transaction_type","site_id","account_code","debit","credit"])

# ---------- OPENING BALANCES ----------
# One balanced journal: all opening lines share a journal ID.
OPENING_DATE=pd.Timestamp("2026-01-01").date()
opening = [
 ("OPEN-2026",OPENING_DATE,"OPENING","OPENING","HO001","1010",6500000,0),
 ("OPEN-2026",OPENING_DATE,"OPENING","OPENING","DC001","1200",8500000,0),
 ("OPEN-2026",OPENING_DATE,"OPENING","OPENING","HO001","1500",12000000,0),
 ("OPEN-2026",OPENING_DATE,"OPENING","OPENING","HO001","1510",4500000,0),
 ("OPEN-2026",OPENING_DATE,"OPENING","OPENING","HO001","1520",1800000,0),
 ("OPEN-2026",OPENING_DATE,"OPENING","OPENING","HO001","1590",0,3000000),
 ("OPEN-2026",OPENING_DATE,"OPENING","OPENING","HO001","2000",0,4500000),
 ("OPEN-2026",OPENING_DATE,"OPENING","OPENING","HO001","2200",0,6000000),
 ("OPEN-2026",OPENING_DATE,"OPENING","OPENING","HO001","3000",0,10000000),
 ("OPEN-2026",OPENING_DATE,"OPENING","OPENING","HO001","3100",0,9800000),
]
gl_df=pd.concat([pd.DataFrame(opening,columns=gl_df.columns),gl_df],ignore_index=True)

# ---------- WRITE ----------
datasets = {
 "site_master.xlsx":sites_df, "department_master.xlsx":departments,
 "product_master.xlsx":products_df, "supplier_master.xlsx":suppliers_df,
 "product_supplier.xlsx":product_suppliers_df, "customer_master.xlsx":customers_df,
 "purchase_orders.xlsx":po_df, "purchase_order_lines.xlsx":po_lines_df,
 "goods_receipts.xlsx":grn_df, "supplier_invoices.xlsx":invoice_df,
 "sales_transactions.xlsx":sales_df, "sales_lines.xlsx":sale_lines_df,
 "operating_expenses.xlsx":expenses_df, "fixed_assets_erp.xlsx":assets_df,
 "branch_local_assets.xlsx":local_assets_df, "supplier_rebates.xlsx":rebates_df,
 "inventory_movements.xlsx":inventory_movements_df, "general_ledger.xlsx":gl_df,
}
for name,df in datasets.items():
    df.to_excel(OUT/name,index=False)

# ---------- VALIDATION ----------
checks = {
 "seed":SEED,
 "files_created":len(datasets),
 "sales_count":len(sales_df),
 "purchase_orders":len(po_df),
 "gl_lines":len(gl_df),
 "gl_total_debits":money(gl_df.debit.sum()),
 "gl_total_credits":money(gl_df.credit.sum()),
 "gl_balanced":abs(gl_df.debit.sum()-gl_df.credit.sum()) < .01,
 "po_moq_violations":int(sum(
     row.po_value < suppliers_df.loc[suppliers_df.supplier_id==row.supplier_id,"minimum_order_value_nad"].iloc[0]
     for row in po_df.itertuples()
 )),
 "duplicate_asset_ids":int(assets_df.asset_id.duplicated().sum()),
}
pd.DataFrame([checks]).to_excel(OUT/"generation_validation.xlsx",index=False)

print("Retail Data Platform v2 synthetic data generated.")
for k,v in checks.items():
    print(f"{k}: {v}")
print(f"Output: {OUT}")
