import dataclasses
import sys
import types
from collections import namedtuple
from typing import Any, Callable, Dict, Iterator, List, Tuple, Union

from typeguard import check_type

from starkware.cairo.lang.compiler.ast.cairo_types import (
    CairoType,
    TypeFelt,
    TypePointer,
    TypeStruct,
    TypeTuple,
)
from starkware.cairo.lang.compiler.parser import parse_type
from starkware.cairo.lang.compiler.type_system import mark_type_resolved
from starkware.python.utils import assert_exhausted, safe_zip
from starkware.starknet.business_logic.execution.objects import OrderedEvent
from starkware.starknet.public.abi import AbiType
from starkware.starknet.testing.contract_utils import (
    RAW_OUTPUT_ARG_LIST,
    EventManager,
    StructManager,
    flatten,
    parse_arguments,
)
from starkware.starknet.testing.objects import Dataclass, StarknetTransactionExecutionInfo
from starkware.starknet.testing.state import CastableToAddress, StarknetState
from starkware.starknet.utils.api_utils import cast_to_felts
from starkware.starknet.business_logic.execution.objects import TransactionExecutionInfo

# Represents Python types, in particular those that are parallel to the cairo ones:
# int, tuple and list (matching the cairo types TypeFelt, TypeTuple/TypeStruct and TypePointer).
PythonType = Any


class StarknetContract:
    """
    A high level interface to a StarkNet contract used for testing. Allows invoking functions.
    Example:
      contract_class = compile_starknet_files(...)
      state = await StarknetState.empty()
      contract_address = await state.deploy(contract_class=contract_class)
      contract = StarknetContract(
          state=state, abi=contract_class.abi, contract_address=contract_address)

      await contract.foo(a=1, b=[2, 3]).invoke()
    """

    def __init__(
        self,
        state: StarknetState,
        abi: AbiType,
        contract_address: CastableToAddress,
        deploy_execution_info: TransactionExecutionInfo,
    ):
        self.state = state
        self.abi = abi
        self.deploy_execution_info = deploy_execution_info

        self.struct_manager = StructManager(abi=abi)
        self.event_manager = EventManager(abi=abi)

        self._abi_function_mapping = {
            abi_entry["name"]: abi_entry for abi_entry in abi if abi_entry["type"] == "function"
        }

        # Cached contract functions.
        self._contract_functions: Dict[str, Callable] = {}

        if isinstance(contract_address, str):
            contract_address = int(contract_address, 16)
        assert isinstance(contract_address, int)
        self.contract_address = contract_address

    def __dir__(self):
        return list(object.__dir__(self)) + list(self._abi_function_mapping.keys())

    def __getattr__(self, name: str):
        if name in self._abi_function_mapping:
            return self.get_contract_function(name=name)
        elif name in self.struct_manager:
            return self.struct_manager.get_contract_struct(name=name)
        else:
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    def get_contract_function(self, name: str) -> Callable:
        """
        Returns a function object that acts as a proxy for a StarkNet contract function.
        """
        if name not in self._contract_functions:
            # Cache contract function.
            self._contract_functions[name] = self._build_contract_function(
                function_abi=self._abi_function_mapping[name]
            )

        return self._contract_functions[name]

    def _build_contract_function(self, function_abi: dict) -> Callable:
        """
        Builds a function object that acts as a proxy for a StarkNet contract function.
        """
        name = function_abi["name"]
        # Parse calldata and retdata arguments.
        arg_names, arg_types = parse_arguments(arguments_abi=function_abi["inputs"])
        retdata_arg_names, retdata_arg_types = parse_arguments(
            arguments_abi=function_abi["outputs"]
        )

        # Build Pythonic type annotations to those arguments, matching their Cairo types.
        # I.e., Cairo Array <> Python List; Cairo tuple/struct <> Python Tuple;
        # Cairo felt <> Python int.
        # This will be added to the contract function info, and be used to validate the structure
        # of the user's input.
        calldata_annotations: Dict[str, PythonType] = {
            name: self._get_annotation(arg_type=arg_type)
            for name, arg_type in safe_zip(arg_names, arg_types)
        }
        retdata_annotations: List[PythonType] = [
            self._get_annotation(arg_type=arg_type) for arg_type in retdata_arg_types
        ]

        def template():
            all_locals = locals()
            args = {arg_name: all_locals[arg_name] for arg_name in arg_names}
            return self._build_function_call(
                function_abi=function_abi,
                calldata_annotations=calldata_annotations,
                args=args,
                retdata_arg_names=retdata_arg_names,
                retdata_arg_types=retdata_arg_types,
            )

        # Create a function like template(), but with extra arguments.
        if sys.version_info.major != 3:
            raise Exception("Must be using Python3.")
        posonlyargcount = (0,) if sys.version_info.minor >= 8 else ()
        func_code = types.CodeType(  # type: ignore
            len(arg_names),  # Arg: argcount.
            *posonlyargcount,  # type: ignore
            0,  # Arg: kwonlyargcount.
            len(arg_names),  # Arg: nlocals.
            template.__code__.co_stacksize + len(arg_names),  # Arg: stacksize.
            template.__code__.co_flags,  # Arg: flags.
            template.__code__.co_code,  # Arg: codestring.
            template.__code__.co_consts,  # Arg: constants.
            template.__code__.co_names,  # Arg: names.
            tuple(arg_names),  # Arg: varnames.
            template.__code__.co_filename,  # Arg: filename.
            name,  # Arg: name.
            template.__code__.co_firstlineno,  # Arg: firstlineno.
            template.__code__.co_lnotab,  # Arg: lnotab.
            template.__code__.co_freevars,  # Arg: freevars.
            template.__code__.co_cellvars,  # Arg: cellvars.
        )

        closure = template.__closure__  # type: ignore
        func = types.FunctionType(code=func_code, globals=globals(), closure=closure)
        func.__annotations__ = {**calldata_annotations, "return": tuple(retdata_annotations)}

        return func

    def _get_annotation(self, arg_type: CairoType, is_nested: bool = False) -> PythonType:
        """
        Returns the Pythonic type annotation of the given Cairo type.
        """
        if isinstance(arg_type, TypeFelt):
            return int
        if isinstance(arg_type, TypePointer):
            assert not is_nested, "Arrays are not supported as members of another type."
            pointee_type = self._get_annotation(arg_type=arg_type.pointee, is_nested=True)
            return List[pointee_type]  # type: ignore
        if isinstance(arg_type, TypeTuple):
            return Tuple[
                tuple(
                    self._get_annotation(arg_type=cairo_type, is_nested=True)
                    for cairo_type in arg_type.types
                )
            ]
        if isinstance(arg_type, TypeStruct):
            struct_def = self.struct_manager.get_struct_definition(name=arg_type.scope.path[-1])
            return Tuple[
                tuple(
                    self._get_annotation(arg_type=member.cairo_type, is_nested=True)
                    for member in struct_def.members.values()
                )
            ]

        raise NotImplementedError

    def _build_function_call(
        self,
        function_abi: dict,
        calldata_annotations: Dict[str, PythonType],
        args: dict,
        retdata_arg_names: List[str],
        retdata_arg_types: List[CairoType],
    ):
        """
        Builds a StarknetContractFunctionInvocation object, representing a call to a StarkNet
        contract with a particular state and set of inputs.
        """
        # Prepare calldata.
        calldata: List[int] = []
        for input_entry in function_abi["inputs"]:
            name = input_entry["name"]
            arg_cairo_type = mark_type_resolved(parse_type(code=input_entry["type"]))
            if name not in args:
                continue

            value = args[name]
            # Checks the full structure of the value.
            check_type(
                argname=f"argument {name}", value=value, expected_type=calldata_annotations[name]
            )
            value = flatten(name=name, value=value)
            if isinstance(arg_cairo_type, TypePointer):
                calldata.append(len(args[name]))

            calldata.extend(value)

        function_name = function_abi["name"]

        return StarknetContractFunctionInvocation(
            state=self.state,
            struct_manager=self.struct_manager,
            event_manager=self.event_manager,
            contract_address=self.contract_address,
            name=function_name,
            calldata=cast_to_felts(values=calldata),
            retdata_arg_types=retdata_arg_types,
            retdata_tuple=namedtuple(f"{function_name}_return_type", retdata_arg_names),
            has_raw_output=(retdata_arg_names == RAW_OUTPUT_ARG_LIST),
        )

    def replace_abi(
        self,
        impl_contract_abi: AbiType,
    ) -> "StarknetContract":
        """
        Replaces the contract's ABI.
        Typically used to replace the ABI of a proxy contract with the ABI of the
        implementation contract.
        """
        return StarknetContract(
            state=self.state,
            abi=impl_contract_abi,
            contract_address=self.contract_address,
            deploy_execution_info=self.deploy_execution_info,
        )


class ArgumentParsingFailed(Exception):
    pass


@dataclasses.dataclass
class StarknetContractFunctionInvocation:
    """
    Represents a call to a StarkNet contract with a particular state and set of inputs.
    """

    state: StarknetState
    struct_manager: StructManager
    event_manager: EventManager
    contract_address: CastableToAddress
    name: str
    calldata: List[int]
    retdata_arg_types: List[CairoType]
    retdata_tuple: type
    has_raw_output: bool

    async def call(
        self, caller_address: int = 0, signature: List[int] = None
    ) -> TransactionExecutionInfo:
        """
        Executes the function call without changing the state.
        """
        return await self._invoke_on_given_state(
            state=self.state.copy(), caller_address=caller_address, signature=signature
        )

    async def invoke(
        self, caller_address: int = 0, max_fee: int = 0, signature: List[int] = None
    ) -> TransactionExecutionInfo:
        """
        Executes the function call and apply changes on the state.
        """
        return await self._invoke_on_given_state(
            state=self.state, caller_address=caller_address, max_fee=max_fee, signature=signature
        )

    async def _invoke_on_given_state(
        self,
        state: StarknetState,
        caller_address: int = 0,
        max_fee: int = 0,
        signature: List[int] = None,
    ) -> TransactionExecutionInfo:
        """
        Executes the function call and apply changes on the given state.
        """
        return await state.invoke_raw(
            contract_address=self.contract_address,
            selector=self.name,
            calldata=self.calldata,
            caller_address=caller_address,
            max_fee=max_fee,
            signature=None if signature is None else cast_to_felts(values=signature),
        )

    def _build_events(self, raw_events: List[OrderedEvent]) -> List[Dataclass]:
        """
        Given a list of low-level events, builds contract events (i.e., a dynamic dataclass) from
        those corresponding to high-level ones.
        """
        events: List[Dataclass] = []
        for raw_event in raw_events:
            if len(raw_event.keys) == 0 or raw_event.keys[0] not in self.event_manager:
                # It is a low-level event emitted using directly the emit_event syscall.
                continue

            selector = raw_event.keys[0]
            arg_values = raw_event.keys[1:] + raw_event.data

            # Try to parse the low-level event as a high-level one (note it is possible for a
            # low-level event to contain a valid selector in its keys without being a valid high
            # level event - i.e., without the exact amount of data).
            try:
                args = self._build_arguments(
                    arg_values=arg_values,
                    arg_types=self.event_manager.get_event_argument_types(identifier=selector),
                )
                args_dataclass = self.event_manager.get_contract_event(identifier=selector)
                events.append(args_dataclass(*args))
            except ArgumentParsingFailed:
                pass

        return events

    def _build_arguments(self, arg_values: List[int], arg_types: List[CairoType]) -> List[Any]:
        """
        Reconstructs a Pythonic variant of the original Cairo structure of the arguments, deduced by
        their Cairo types, and fills it with the given (flat list of) values.
        """

        def build_arg(
            arg_type: CairoType, arg_value_iterator: Iterator[int]
        ) -> Union[int, tuple, List[Any]]:
            """
            Reconstructs a Pythonic variant of the original Cairo structure of the given argument.
            """
            if isinstance(arg_type, TypeFelt):
                return next(arg_value_iterator)
            if isinstance(arg_type, TypeTuple):
                return tuple(
                    build_arg(arg_type=cairo_type, arg_value_iterator=arg_value_iterator)
                    for cairo_type in arg_type.types
                )
            if isinstance(arg_type, TypeStruct):
                struct_name = arg_type.scope.path[-1]
                struct_def = self.struct_manager.get_struct_definition(name=struct_name)
                contract_struct = self.struct_manager.get_contract_struct(name=struct_name)
                return contract_struct(
                    *(
                        build_arg(arg_type=member.cairo_type, arg_value_iterator=arg_value_iterator)
                        for member in struct_def.members.values()
                    )
                )
            if isinstance(arg_type, TypePointer):
                arr_len = next(arg_value_iterator)
                return [
                    build_arg(arg_type=arg_type.pointee, arg_value_iterator=arg_value_iterator)
                    for _ in range(arr_len)
                ]

            raise NotImplementedError

        arg_value_iterator = iter(arg_values)

        try:
            res = [
                build_arg(arg_type=arg_type, arg_value_iterator=arg_value_iterator)
                for arg_type in arg_types
            ]
        except StopIteration:
            raise ArgumentParsingFailed("Too few argument values.")

        # Make sure the iterator is empty.
        try:
            assert_exhausted(iterator=arg_value_iterator)
        except AssertionError:
            raise ArgumentParsingFailed("Too many argument values.")

        return res


@dataclasses.dataclass(frozen=True)
class DeclaredClass:
    """
    A helper class that bundles conveniently the return value of declare().
    """

    class_hash: int
    abi: AbiType
