"""Extension contract only; NOT an implemented Alexa/Google integration.
A provider-specific adapter must verify signatures, prevent replay, and normalize
its actual provider payload before calling Room Hub's input-only API.
"""
from dataclasses import dataclass,field
from typing import Protocol,Mapping,Any
@dataclass(frozen=True)
class TranscriptEnvelope:
    request_id:str
    source:str
    text:str
    locale:str='ko-KR'
    metadata:dict[str,Any]=field(default_factory=dict)
    schema_version:str='1'
class ProviderAdapter(Protocol):
    def verify(self,headers:Mapping[str,str],body:bytes)->bool:
        """Validate provider signatures/timestamps; never blindly return True."""
        ...
    def normalize(self,headers:Mapping[str,str],body:bytes)->TranscriptEnvelope:
        """Map provider intent/slot/text fields, not assumed raw speech audio."""
        ...
