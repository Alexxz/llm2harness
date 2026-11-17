#!/usr/bin/env python3
"""One offline inference or a bounded real LoRA training episode; JSON lines out."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ['HF_HOME']=str(ROOT/'work/hf-home')
os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN']='1'
os.environ['HF_HUB_OFFLINE']='1'
from tools.make_training_data import MODEL_ID,MODEL_REVISION


def emit(event,**data):
    print(json.dumps({'event':event,**data},ensure_ascii=False),flush=True)


def main():
    request=json.loads(sys.stdin.buffer.read(262144))
    operation=request['operation'];config=request['config'];output=Path(request['adapter_dir'])
    if operation not in {'before','train','after','probe','off'}:raise ValueError('Unknown operation')
    if not 1<=config['steps']<=32 or not 0<config['learning_rate']<=.01 or not 1<=config['rank']<=8 or not 1<=config['max_new_tokens']<=128 or not 1<=config['max_seconds']<=180:raise ValueError('Episode budget is invalid')
    if not isinstance(config.get('pairs'),list) or len(config['pairs'])!=4 or any(not isinstance(p.get(k),str) or not p[k].strip() or len(p[k].encode())>2048 for p in config['pairs'] for k in ('request','answer')):
        raise ValueError('Exactly four bounded teaching pairs are required')
    if config['new_request'] in {p['request'] for p in config['pairs']}:raise ValueError('The new request must be outside the teaching pairs')
    import torch
    from transformers import AutoModelForCausalLM,AutoTokenizer,set_seed
    torch.set_num_threads(4);set_seed(42)
    args=dict(revision=MODEL_REVISION,cache_dir=str(ROOT/'work/model-cache'),local_files_only=True,token=False,trust_remote_code=False)
    started=time.monotonic();emit('loading',message='Загружаются подготовленные локальные веса')
    tokenizer=AutoTokenizer.from_pretrained(MODEL_ID,**args)
    model=AutoModelForCausalLM.from_pretrained(MODEL_ID,torch_dtype=torch.float32,use_safetensors=True,**args).to('cpu')
    messages=lambda text:[{'role':'system','content':config['instruction']},{'role':'user','content':text}]
    if operation=='train':
        from peft import LoraConfig,TaskType,get_peft_model
        if output.exists():raise ValueError('Training output already exists; no overwrite')
        model.config.use_cache=False
        model=get_peft_model(model,LoraConfig(task_type=TaskType.CAUSAL_LM,r=config['rank'],lora_alpha=config['rank']*2,lora_dropout=0,target_modules=['q_proj','v_proj'],bias='none'))
        trainable=[(name,p) for name,p in model.named_parameters() if p.requires_grad]
        if not trainable or any('lora_' not in name for name,_ in trainable):raise ValueError('Only LoRA parameters may be trained')
        def digest():
            h=hashlib.sha256()
            for name,p in trainable:h.update(name.encode());h.update(p.detach().float().cpu().numpy().tobytes())
            return h.hexdigest()
        encoded=[]
        for pair in config['pairs']:
            prefix=tokenizer.apply_chat_template(messages(pair['request']),tokenize=True,add_generation_prompt=True)
            full=tokenizer.apply_chat_template(messages(pair['request'])+[{'role':'assistant','content':pair['answer']}],tokenize=True,add_generation_prompt=False)
            if full[:len(prefix)]!=prefix or len(full)>256:raise ValueError('Training example exceeds limits or has an invalid answer mask')
            encoded.append((torch.tensor([full]),torch.tensor([[-100]*len(prefix)+full[len(prefix):]])))
        before=digest();optimizer=torch.optim.AdamW([p for _,p in trainable],lr=config['learning_rate'],weight_decay=0)
        model.train();losses=[];training_start=time.monotonic()
        for step in range(config['steps']):
            if time.monotonic()-training_start>config['max_seconds']:raise ValueError('Training time budget exhausted; no finished adapter')
            ids,labels=encoded[step%len(encoded)];optimizer.zero_grad(set_to_none=True)
            loss=model(input_ids=ids,attention_mask=torch.ones_like(ids),labels=labels).loss
            if not torch.isfinite(loss):raise ValueError('Non-finite training loss')
            loss.backward();torch.nn.utils.clip_grad_norm_([p for _,p in trainable],1);optimizer.step()
            losses.append(float(loss.detach()));emit('progress',step=step+1,total=config['steps'],loss=losses[-1],seconds=round(time.monotonic()-training_start,2))
        after=digest()
        if before==after:raise ValueError('Trainable parameters did not change')
        output.mkdir(parents=True);model.save_pretrained(output,safe_serialization=True)
        with (output/'adapter_model.safetensors').open('rb') as stream:weights=hashlib.file_digest(stream,'sha256').hexdigest()
        report={'base_model':MODEL_ID,'base_revision':MODEL_REVISION,'dtype':'float32','device':'cpu','seed':42,'steps':len(losses),'pairs':config['pairs'],'losses':losses,'parameter_digest_before':before,'parameter_digest_after':after,'adapter_weights_sha256':weights,'trainable_parameters':sum(p.numel() for _,p in trainable),'seconds':round(time.monotonic()-started,3),'training_seconds':round(time.monotonic()-training_start,3),'completed':True}
        (output/'episode-manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        emit('result',result=report);return
    if operation in {'after','probe'}:
        from peft import PeftModel
        manifest=json.loads((output/'episode-manifest.json').read_text())
        if manifest['pairs']!=config['pairs']:raise ValueError('Teaching pairs changed after training')
        if manifest['base_model']!=MODEL_ID or manifest['base_revision']!=MODEL_REVISION or not manifest['completed']:raise ValueError('Adapter provenance mismatch')
        with (output/'adapter_model.safetensors').open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
        if actual!=manifest['adapter_weights_sha256']:raise ValueError('Adapter checksum mismatch')
        model=PeftModel.from_pretrained(model,output,is_trainable=False)
    text=config['new_request'] if operation=='probe' else config['request']
    prompt=tokenizer.apply_chat_template(messages(text),tokenize=False,add_generation_prompt=True)
    inputs=tokenizer(prompt,return_tensors='pt',add_special_tokens=False)
    if inputs['input_ids'].shape[-1]+config['max_new_tokens']>512:raise ValueError('Inference context limit')
    model.eval()
    with torch.inference_mode():tokens=model.generate(**inputs,max_new_tokens=config['max_new_tokens'],do_sample=False,pad_token_id=tokenizer.eos_token_id)
    tail=tokens[0,inputs['input_ids'].shape[-1]:]
    emit('result',result={'request':text,'prompt':prompt,'text':tokenizer.decode(tail,skip_special_tokens=True),'adapter':operation in {'after','probe'},'base_model':MODEL_ID,'base_revision':MODEL_REVISION,'settings':{'seed':42,'do_sample':False,'max_new_tokens':config['max_new_tokens'],'dtype':'float32','device':'cpu'},'seconds':round(time.monotonic()-started,3),'tokens':len(tail)})


if __name__=='__main__':
    try:main()
    except Exception as error:
        emit('error',error_type=type(error).__name__,message=str(error)[:400]);raise SystemExit(1)
