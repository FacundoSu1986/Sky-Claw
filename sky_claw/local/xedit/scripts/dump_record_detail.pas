{
  dump_record_detail.pas — xEdit Pascal script for the AI advisor (Fase 1).

  For every CRITICAL-type record that has overrides, dumps each version's
  top-level elements (master + overrides) so PatchAdvisorLLM can summarize
  the differing subrecords for the operator. READ-ONLY: never modifies any
  loaded plugin.

  Output format (pipe-delimited via AddMessage, same protocol family as
  list_all_conflicts.pas — '|' is invalid in Windows filenames):

    DUMP_BEGIN|<FormID>|<EditorID>|<RecordType>
    VERSION|<FormID>|<Plugin>|<1 if winning override, else 0>
    ELEMENT|<FormID>|<Plugin>|<ElementPath>|<Value>
    DUMP_END|<FormID>

  Parsed by sky_claw/local/xedit/record_dump_parser.py — keep both sides in
  sync. Values are sanitized ('|' -> '/', newlines -> ' ') and truncated to
  MAX_VALUE_CHARS; at most MAX_ELEMENTS elements are emitted per version
  (QUST/CELL records can be huge and the advisor only needs a bounded diff).

  NOTE: like the other bundled scripts, this needs a smoke run against a
  real xEdit install before trusting it 100% (see docs/pending_ooda_status.md).
}
unit dump_record_detail;

const
  MAX_ELEMENTS = 40;
  MAX_VALUE_CHARS = 200;

var
  dumpedRecords: Integer;

{ Keep in sync with IsCriticalType in list_all_conflicts.pas (anchored by
  tests/test_conflict_signatures_sync.py on the Python side). }
function IsCriticalType(sig: string): Boolean;
begin
  Result := (sig = 'NPC_') or (sig = 'QUST') or (sig = 'SCEN') or
            (sig = 'INFO') or (sig = 'PERK') or (sig = 'SPEL') or
            (sig = 'MGEF') or (sig = 'FACT') or (sig = 'DIAL') or
            (sig = 'PACK');
end;

{ Sanitize a value for the pipe-delimited protocol: no '|' (separator), no
  newlines (one line per element), bounded length. }
function SanitizeValue(s: string): string;
begin
  Result := StringReplace(s, '|', '/', [rfReplaceAll]);
  Result := StringReplace(Result, #13, ' ', [rfReplaceAll]);
  Result := StringReplace(Result, #10, ' ', [rfReplaceAll]);
  if Length(Result) > MAX_VALUE_CHARS then
    Result := Copy(Result, 1, MAX_VALUE_CHARS);
end;

{ Emit the top-level elements of ONE record version. GetEditValue returns ''
  for containers; those are skipped — the advisor works on leaf-ish values
  and a bounded second level would balloon QUST dumps. }
procedure EmitVersionElements(rec: IInterface; formID, plugin: string);
var
  i, emitted: Integer;
  el: IInterface;
  value: string;
begin
  emitted := 0;
  for i := 0 to ElementCount(rec) - 1 do begin
    if emitted >= MAX_ELEMENTS then begin
      AddMessage('ELEMENT|' + formID + '|' + plugin + '|(truncated)|...');
      Exit;
    end;
    el := ElementByIndex(rec, i);
    value := GetEditValue(el);
    if value = '' then
      Continue;
    AddMessage('ELEMENT|' + formID + '|' + plugin + '|' +
               SanitizeValue(Name(el)) + '|' + SanitizeValue(value));
    Inc(emitted);
  end;
end;

{ Emit one VERSION line + its elements. }
procedure EmitVersion(rec: IInterface; formID, winner: string);
var
  plugin, isWinner: string;
begin
  plugin := GetFileName(GetFile(rec));
  if plugin = winner then
    isWinner := '1'
  else
    isWinner := '0';
  AddMessage('VERSION|' + formID + '|' + plugin + '|' + isWinner);
  EmitVersionElements(rec, formID, plugin);
end;

function Initialize: Integer;
begin
  dumpedRecords := 0;
  Result := 0;
end;

function Process(e: IInterface): Integer;
var
  i: Integer;
  sig, formID, winner: string;
begin
  Result := 0;

  { Same filter discipline as list_all_conflicts.pas: master records with
    overrides only, and only critical-type signatures (the advisor's target
    set — dumping WEAP/ARMO warnings would balloon the output for nothing). }
  if OverrideCount(e) < 1 then
    Exit;
  if not IsMaster(e) then
    Exit;
  sig := Signature(e);
  if not IsCriticalType(sig) then
    Exit;

  formID := IntToHex(FormID(e), 8);
  winner := GetFileName(GetFile(WinningOverride(e)));

  AddMessage('DUMP_BEGIN|' + formID + '|' + EditorID(e) + '|' + sig);
  EmitVersion(e, formID, winner);
  for i := 0 to OverrideCount(e) - 1 do
    EmitVersion(OverrideByIndex(e, i), formID, winner);
  AddMessage('DUMP_END|' + formID);

  Inc(dumpedRecords);
end;

function Finalize: Integer;
begin
  AddMessage('DUMP_SUMMARY|records=' + IntToStr(dumpedRecords));
  Result := 0;
end;

end.
