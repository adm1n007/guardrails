import asyncio
import contextvars
import json
import os
from builtins import id as object_id
from copy import deepcopy
from string import Template
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Generic,
    Iterable,
    List,
    Optional,
    Sequence,
    Type,
    Union,
    cast,
    overload,
)

from guardrails_api_client import (
    Guard as IGuard,
    GuardHistory,
    ValidatorReference,
    ModelSchema,
    ValidatePayload,
    ValidationType,
    SimpleTypes,
)
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable, RunnableConfig
from pydantic import Field, field_validator

from guardrails.api_client import GuardrailsApiClient
from guardrails.classes.output_type import OT
from guardrails.classes.input_type import InputType
from guardrails.classes.validation_outcome import ValidationOutcome
from guardrails.classes.validation.validation_result import FailResult
from guardrails.classes.credentials import Credentials
from guardrails.classes.execution import GuardExecutionOptions
from guardrails.classes.generic import Stack
from guardrails.classes.history import Call
from guardrails.classes.history.call_inputs import CallInputs
from guardrails.classes.history.inputs import Inputs
from guardrails.classes.history.iteration import Iteration
from guardrails.classes.history.outputs import Outputs
from guardrails.classes.output_type import OutputTypes
from guardrails.classes.schema.processed_schema import ProcessedSchema
from guardrails.errors import ValidationError
from guardrails.llm_providers import (
    get_async_llm_ask,
    get_llm_api_enum,
    get_llm_ask,
    model_is_supported_server_side,
)
from guardrails.logger import logger, set_scope
from guardrails.prompt import Instructions, Prompt
from guardrails.rail import Rail
from guardrails.run import AsyncRunner, Runner, StreamRunner
from guardrails.schema.primitive_schema import primitive_to_schema
from guardrails.schema.pydantic_schema import pydantic_model_to_schema
from guardrails.schema.rail_schema import rail_file_to_schema, rail_string_to_schema
from guardrails.schema.validator import SchemaValidationError, validate_json_schema
from guardrails.stores.context import (
    Tracer,
    get_call_kwarg,
    get_tracer_context,
    set_call_kwargs,
    set_tracer,
    set_tracer_context,
)
from guardrails.types.pydantic import ModelOrListOfModels
from guardrails.utils.naming_utils import random_id
from guardrails.utils.safe_get import safe_get
from guardrails.utils.hub_telemetry_utils import HubTelemetry
from guardrails.classes.llm.llm_response import LLMResponse
from guardrails.utils.reask_utils import FieldReAsk
from guardrails.utils.validator_utils import get_validator, verify_metadata_requirements
from guardrails.validator_base import Validator
from guardrails.types import (
    UseManyValidatorTuple,
    UseManyValidatorSpec,
    UseValidatorSpec,
    ValidatorMap,
)


class Guard(IGuard, Runnable, Generic[OT]):
    """The Guard class.

    This class is the main entry point for using Guardrails. It can be
    initialized by one of the following patterns:

    - `Guard().use(...)`
    - `Guard().use_many(...)`
    - `Guard.from_string(...)`
    - `Guard.from_pydantic(...)`
    - `Guard.from_rail(...)`
    - `Guard.from_rail_string(...)`

    The `__call__`
    method functions as a wrapper around LLM APIs. It takes in an LLM
    API, and optional prompt parameters, and returns a ValidationOutcome
    class that contains the raw output from
    the LLM, the validated output, as well as other helpful information.
    """

    # Public
    id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    validators: Optional[List[ValidatorReference]] = []
    output_schema: Optional[ModelSchema] = None

    # Legacy
    _num_reasks = None
    _rail: Optional[Rail] = None
    _base_model: Optional[ModelOrListOfModels]

    # Private
    _tracer = None
    _tracer_context = None
    _hub_telemetry = None
    _user_id = None
    _validator_map: ValidatorMap = {}
    _validators: List[Validator] = []
    _api_client: Optional[GuardrailsApiClient] = None
    _allow_metrics_collection: Optional[bool] = None
    _exec_opts: GuardExecutionOptions
    _output_type: OutputTypes

    def __init__(
        self,
        *,
        id: Optional[str] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        validators: Optional[List[ValidatorReference]] = [],
        output_schema: Optional[Dict[str, Any]] = {},
    ):
        """Initialize the Guard with optional Rail instance, num_reasks, and
        base_model."""

        # Shared Interface Properties
        id = id or random_id()
        name = name or f"gr-{id}"

        # Init ModelSchema class
        schema_with_type = {**output_schema}
        output_schema_type = output_schema.get("type")
        if output_schema_type:
            schema_with_type["type"] = ValidationType.from_dict(output_schema_type)
        model_schema = ModelSchema(**schema_with_type)

        # Super Init
        super().__init__(
            id=id,
            name=name,
            description=description,
            validators=validators,
            output_schema=model_schema,
            history=GuardHistory([]),
        )

        # Assign private properties and backfill
        self._validator_map = {}
        self._validators = []
        self._fill_validator_map()
        self._fill_validators()

        # TODO: Support a sink for history so that it is not solely held in memory
        self._history: Stack[Call] = Stack()

        # Gaurdrails As A Service Initialization
        api_key = os.environ.get("GUARDRAILS_API_KEY")
        if api_key is not None:
            self._api_client = GuardrailsApiClient(api_key=api_key)
            self.upsert_guard()

    # FIXME
    @property
    def history(self):
        return self._history

    # FIXME
    @history.setter
    def history(self, h: Stack[Call]):
        self._history = h

    @field_validator("output_schema")
    @classmethod
    def must_be_valid_json_schema(
        cls, output_schema: Optional[ModelSchema] = None
    ) -> Optional[ModelSchema]:
        if output_schema:
            try:
                validate_json_schema(output_schema.to_dict())
            except SchemaValidationError as e:
                raise ValueError(f"{str(e)}\n{json.dumps(e.fields, indent=2)}")
        return output_schema

    def configure(
        self,
        *,
        num_reasks: Optional[int] = None,
        tracer: Optional[Tracer] = None,
        allow_metrics_collection: Optional[bool] = None,
    ):
        """Configure the Guard."""
        if num_reasks:
            self._set_num_reasks(num_reasks)
        if tracer:
            self._set_tracer(tracer)
        self._configure_telemtry(allow_metrics_collection)

    def _set_num_reasks(self, num_reasks: int = 1):
        self._num_reasks = num_reasks

    def _set_tracer(self, tracer: Optional[Tracer] = None) -> None:
        self._tracer = tracer
        set_tracer(tracer)
        set_tracer_context()
        self._tracer_context = get_tracer_context()

    def _configure_telemtry(
        self, allow_metrics_collection: Optional[bool] = None
    ) -> None:
        if allow_metrics_collection is None:
            credentials = Credentials.from_rc_file(logger)
            allow_metrics_collection = credentials.no_metrics is False

        self._allow_metrics_collection = allow_metrics_collection

        if allow_metrics_collection:
            # Get unique id of user from credentials
            self._user_id = credentials.id or ""
            # Initialize Hub Telemetry singleton and get the tracer
            self._hub_telemetry = HubTelemetry()

    def _fill_validator_map(self):
        for ref in self.validators:
            entry: List[Validator] = self._validator_map.get(ref.on, [])
            # Check if the validator from the reference
            #   has an instance in the validator_map
            v = safe_get(
                list(
                    filter(
                        lambda v: (
                            v.rail_alias == ref.id
                            and v.on_fail_descriptor == ref.on_fail
                            and v.get_args() == ref.kwargs
                        ),
                        entry,
                    )
                ),
                0,
            )
            if not v:
                serialized_args = list(
                    map(
                        lambda arg: Template("{${arg}}").safe_substitute(arg=arg),
                        ref.kwargs.values(),
                    )
                )
                string_syntax = (
                    Template("${id}: ${args}").safe_substitute(
                        id=ref.id, args=" ".join(serialized_args)
                    )
                    if len(serialized_args) > 0
                    else ref.id
                )
                entry.append(get_validator((string_syntax, ref.on_fail)))
                self._validator_map[ref.on] = entry

    def _fill_validators(self):
        self._validators = [
            v
            for v_list in [self._validator_map[k] for k in self._validator_map]
            for v in v_list
        ]

    # FIXME: What do we have this to look like now?
    def __repr__(self):
        return f"Guard(RAIL={self.rail})"

    # FIXME: What do we have this to look like now?
    def __rich_repr__(self):
        yield "RAIL", self.rail

    def __stringify__(self):
        if self._output_type == OutputTypes.STRING:
            template = Template(
                """
                Guard {
                    validators: [
                        ${validators}
                    ]
                }
                    """
            )
            return template.safe_substitute(
                {
                    "validators": ",\n".join(
                        [v.__stringify__() for v in self._validators]
                    )
                }
            )
        return self.__repr__()

    @classmethod
    def _from_rail_schema(
        cls,
        schema: ProcessedSchema,
        rail: str,
        *,
        num_reasks: Optional[int] = None,
        tracer: Optional[Tracer] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        guard = cls(
            name=name,
            description=description,
            output_schema=schema.json_schema,
            validators=schema.validators,
        )
        if schema.output_type == OutputTypes.STRING:
            guard = cast(Guard[str], guard)
        elif schema.output_type == OutputTypes.LIST:
            guard = cast(Guard[List], guard)
        else:
            guard = cast(Guard[Dict], guard)
        guard.configure(num_reasks=num_reasks, tracer=tracer)
        guard._validator_map = schema.validator_map
        guard._exec_opts = schema.exec_opts
        guard._output_type = schema.output_type
        guard._rail = rail
        return guard

    @classmethod
    def from_rail(
        cls,
        rail_file: str,
        *,
        num_reasks: Optional[int] = Field(
            default=None,
            deprecated=(
                "Setting num_reasks during initialization is deprecated"
                " and will be removed in 0.6.x!"
                "We recommend setting num_reasks when calling guard()"
                " or guard.parse() instead."
                "If you insist on setting it at the Guard level,"
                " use 'Guard.configure()'."
            ),
        ),
        tracer: Optional[Tracer] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        """Create a Schema from a `.rail` file.

        Args:
            rail_file: The path to the `.rail` file.
            num_reasks (int, optional): The max times to re-ask the LLM if validation fails.
            tracer (Tracer, optional): An OpenTelemetry tracer to use for metrics and traces. Defaults to None.
            name (str, optional): A unique name for this Guard. Defaults to `gr-` + the object id.
            description (str, optional): A description for this Guard. Defaults to None.

        Returns:
            An instance of the `Guard` class.
        """  # noqa

        # We have to set the tracer in the ContextStore before the Rail,
        #   and therefore the Validators, are initialized
        cls._set_tracer(cls, tracer)  # type: ignore

        schema = rail_file_to_schema(rail_file)
        return cls._from_rail_schema(
            schema,
            rail=rail_file,
            num_reasks=num_reasks,
            tracer=tracer,
            name=name,
            description=description,
        )

    @classmethod
    def from_rail_string(
        cls,
        rail_string: str,
        *,
        num_reasks: Optional[int] = Field(
            default=None,
            deprecated=(
                "Setting num_reasks during initialization is deprecated"
                " and will be removed in 0.6.x!"
                "We recommend setting num_reasks when calling guard()"
                " or guard.parse() instead."
                "If you insist on setting it at the Guard level,"
                " use 'Guard.configure()'."
            ),
        ),
        tracer: Optional[Tracer] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        """Create a Schema from a `.rail` string.

        Args:
            rail_string: The `.rail` string.
            num_reasks (int, optional): The max times to re-ask the LLM if validation fails.
            tracer (Tracer, optional): An OpenTelemetry tracer to use for metrics and traces. Defaults to None.
            name (str, optional): A unique name for this Guard. Defaults to `gr-` + the object id.
            description (str, optional): A description for this Guard. Defaults to None.

        Returns:
            An instance of the `Guard` class.
        """  # noqa
        # We have to set the tracer in the ContextStore before the Rail,
        #   and therefore the Validators, are initialized
        cls._set_tracer(cls, tracer)  # type: ignore

        schema = rail_string_to_schema(rail_string)
        return cls._from_rail_schema(
            schema,
            rail=rail_string,
            num_reasks=num_reasks,
            tracer=tracer,
            name=name,
            description=description,
        )

    @classmethod
    def from_pydantic(
        cls,
        output_class: ModelOrListOfModels,
        *,
        prompt: Optional[str] = None,
        instructions: Optional[str] = None,
        num_reasks: Optional[int] = Field(
            default=None,
            deprecated=(
                "Setting num_reasks during initialization is deprecated"
                " and will be removed in 0.6.x!"
                "We recommend setting num_reasks when calling guard()"
                " or guard.parse() instead."
                "If you insist on setting it at the Guard level,"
                " use 'Guard.configure()'."
            ),
        ),
        reask_prompt: Optional[str] = None,
        reask_instructions: Optional[str] = None,
        tracer: Optional[Tracer] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        """Create a Guard instance from a Pydantic model.

        Args:
            output_class: (Union[Type[BaseModel], List[Type[BaseModel]]]): The pydantic model that describes
            the desired structure of the output.
            prompt (str, optional): The prompt used to generate the string. Defaults to None.
            instructions (str, optional): Instructions for chat models. Defaults to None.
            reask_prompt (str, optional): An alternative prompt to use during reasks. Defaults to None.
            reask_instructions (str, optional): Alternative instructions to use during reasks. Defaults to None.
            num_reasks (int, optional): The max times to re-ask the LLM if validation fails. Deprecated
            tracer (Tracer, optional): An OpenTelemetry tracer to use for metrics and traces. Defaults to None.
            name (str, optional): A unique name for this Guard. Defaults to `gr-` + the object id.
            description (str, optional): A description for this Guard. Defaults to None.
        """  # noqa
        # We have to set the tracer in the ContextStore before the Rail,
        #   and therefore the Validators, are initialized
        cls._set_tracer(cls, tracer)  # type: ignore

        schema = pydantic_model_to_schema(output_class)
        exec_opts = GuardExecutionOptions(
            prompt=prompt,
            instructions=instructions,
            reask_prompt=reask_prompt,
            reask_instructions=reask_instructions,
        )
        guard = cls(
            name=name,
            description=description,
            output_schema=schema.json_schema,
            validators=schema.validators,
        )
        if schema.output_type == OutputTypes.LIST:
            guard = cast(Guard[List], guard)
        else:
            guard = cast(Guard[Dict], guard)
        guard.configure(num_reasks=num_reasks, tracer=tracer)
        guard._validator_map = schema.validator_map
        guard._exec_opts = exec_opts
        guard._output_type = schema.output_type
        guard._base_model = output_class
        return guard

    @classmethod
    def from_string(
        cls,
        validators: Sequence[Validator],
        *,
        string_description: Optional[str] = None,
        prompt: Optional[str] = None,
        instructions: Optional[str] = None,
        reask_prompt: Optional[str] = None,
        reask_instructions: Optional[str] = None,
        num_reasks: Optional[int] = None,
        tracer: Optional[Tracer] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        """Create a Guard instance for a string response.

        Args:
            validators: (List[Validator]): The list of validators to apply to the string output.
            string_description (str, optional): A description for the string to be generated. Defaults to None.
            prompt (str, optional): The prompt used to generate the string. Defaults to None.
            instructions (str, optional): Instructions for chat models. Defaults to None.
            reask_prompt (str, optional): An alternative prompt to use during reasks. Defaults to None.
            reask_instructions (str, optional): Alternative instructions to use during reasks. Defaults to None.
            num_reasks (int, optional): The max times to re-ask the LLM if validation fails.
            tracer (Tracer, optional): An OpenTelemetry tracer to use for metrics and traces. Defaults to None.
            name (str, optional): A unique name for this Guard. Defaults to `gr-` + the object id.
            description (str, optional): A description for this Guard. Defaults to None.
        """  # noqa

        # This might not be necessary anymore
        cls._set_tracer(cls, tracer)  # type: ignore

        schema = primitive_to_schema(
            validators, type=SimpleTypes.STRING, description=string_description
        )
        exec_opts = GuardExecutionOptions(
            prompt=prompt,
            instructions=instructions,
            reask_prompt=reask_prompt,
            reask_instructions=reask_instructions,
        )
        guard = cast(
            Guard[str],
            cls(
                name=name,
                description=description,
                output_schema=schema.json_schema,
                validators=schema.validators,
            ),
        )
        guard.configure(num_reasks=num_reasks, tracer=tracer)
        guard._validator_map = schema.validator_map
        guard._exec_opts = exec_opts
        guard._output_type = schema.output_type
        return guard

    def _execute(
        self,
        *args,
        llm_api: Optional[Union[Callable, Callable[[Any], Awaitable[Any]]]] = None,
        llm_output: Optional[str] = None,
        prompt_params: Optional[Dict] = None,
        num_reasks: Optional[int] = None,
        prompt: Optional[str] = None,
        instructions: Optional[str] = None,
        msg_history: Optional[List[Dict]] = None,
        metadata: Optional[Dict] = {},
        full_schema_reask: Optional[bool] = None,
        **kwargs,
    ) -> Union[
        Union[ValidationOutcome[OT], Iterable[ValidationOutcome[OT]]],
        Awaitable[ValidationOutcome[OT]],
    ]:
        if not llm_api and not llm_output:
            raise RuntimeError("'llm_api' or 'llm_output' must be provided!")
        if not llm_output and llm_api and not (prompt or msg_history):
            raise RuntimeError(
                "'prompt' or 'msg_history' must be provided in order to call an LLM!"
            )

        # check if validator requirements are fulfilled
        missing_keys = verify_metadata_requirements(metadata, self._validators)
        if missing_keys:
            raise ValueError(
                f"Missing required metadata keys: {', '.join(missing_keys)}"
            )

        def __exec(
            self: Guard,
            *args,
            llm_api: Union[Callable, Callable[[Any], Awaitable[Any]]],
            llm_output: Optional[str] = None,
            prompt_params: Optional[Dict] = {},
            num_reasks: Optional[int] = None,
            prompt: Optional[str] = None,
            instructions: Optional[str] = None,
            msg_history: Optional[List[Dict]] = None,
            metadata: Optional[Dict] = {},
            full_schema_reask: Optional[bool] = None,
            **kwargs,
        ):
            if full_schema_reask is None:
                full_schema_reask = self._base_model is not None

            if self._allow_metrics_collection:
                # Create a new span for this guard call
                self._hub_telemetry.create_new_span(
                    span_name="/guard_call",
                    attributes=[
                        ("guard_id", self.id),
                        ("user_id", self._user_id),
                        ("llm_api", llm_api.__name__ if llm_api else "None"),
                        (
                            "custom_reask_prompt",
                            self._exec_opts.reask_prompt is not None,
                        ),
                        (
                            "custom_reask_instructions",
                            self._exec_opts.reask_instructions is not None,
                        ),
                    ],
                    is_parent=True,  # It will have children
                    has_parent=False,  # Has no parents
                )

            set_call_kwargs(kwargs)
            set_tracer(self._tracer)
            set_tracer_context(self._tracer_context)

            self._set_num_reasks(num_reasks=num_reasks)
            if self._num_reasks is None:
                raise RuntimeError(
                    "`num_reasks` is `None` after calling `configure()`. "
                    "This should never happen."
                )

            input_prompt = prompt or self._exec_opts.prompt
            input_instructions = instructions or self._exec_opts.instructions
            call_inputs = CallInputs(
                llm_api=llm_api,
                prompt=input_prompt,
                instructions=input_instructions,
                msg_history=msg_history,
                prompt_params=prompt_params,
                num_reasks=self._num_reasks,
                metadata=metadata,
                full_schema_reask=full_schema_reask,
                args=list(args),
                kwargs=kwargs,
            )
            call_log = Call(inputs=call_inputs)
            set_scope(str(object_id(call_log)))
            self._history.push(call_log)

            if self._api_client is not None and model_is_supported_server_side(
                llm_api, *args, **kwargs
            ):
                return self._call_server(
                    llm_output=llm_output,
                    llm_api=llm_api,
                    num_reasks=self._num_reasks,
                    prompt_params=prompt_params,
                    full_schema_reask=full_schema_reask,
                    call_log=call_log,
                    *args,
                    **kwargs,
                )

            # If the LLM API is async, return a coroutine
            if asyncio.iscoroutinefunction(llm_api):
                return self._exec_async(
                    llm_api=llm_api,
                    llm_output=llm_output,
                    prompt_params=prompt_params,
                    num_reasks=self._num_reasks,
                    prompt=prompt,
                    instructions=instructions,
                    msg_history=msg_history,
                    metadata=metadata,
                    full_schema_reask=full_schema_reask,
                    call_log=call_log,
                    *args,
                    **kwargs,
                )
            # Otherwise, call the LLM synchronously
            return self._exec_sync(
                llm_api=llm_api,
                llm_output=llm_output,
                prompt_params=prompt_params,
                num_reasks=self._num_reasks,
                prompt=prompt,
                instructions=instructions,
                msg_history=msg_history,
                metadata=metadata,
                full_schema_reask=full_schema_reask,
                call_log=call_log,
                *args,
                **kwargs,
            )

        guard_context = contextvars.Context()
        return guard_context.run(
            __exec,
            self,
            llm_api=llm_api,
            llm_output=llm_output,
            prompt_params=prompt_params,
            num_reasks=num_reasks,
            prompt=prompt,
            instructions=instructions,
            msg_history=msg_history,
            metadata=metadata,
            full_schema_reask=full_schema_reask,
            *args,
            **kwargs,
        )

    def _exec_sync(
        self,
        *args,
        llm_api: Optional[Callable] = None,
        llm_output: Optional[str] = None,
        call_log: Call,  # Not optional, but internal
        prompt_params: Dict = {},  # Should be defined at this point
        num_reasks: int = 0,  # Should be defined at this point
        metadata: Dict = {},  # Should be defined at this point
        full_schema_reask: bool = False,  # Should be defined at this point
        prompt: Optional[str] = None,
        instructions: Optional[str] = None,
        msg_history: Optional[List[Dict]] = None,
        **kwargs,
    ) -> Union[ValidationOutcome[OT], Iterable[ValidationOutcome[OT]]]:
        api = get_llm_ask(llm_api, *args, **kwargs) if llm_api is not None else None

        # Check whether stream is set
        if kwargs.get("stream", False):
            # If stream is True, use StreamRunner
            runner = StreamRunner(
                output_type=self._output_type,
                output_schema=self.output_schema.to_dict(),
                num_reasks=num_reasks,
                validation_map=self._validator_map,
                prompt=prompt,
                instructions=instructions,
                msg_history=msg_history,
                api=api,
                metadata=metadata,
                output=llm_output,
                base_model=self._base_model,
                full_schema_reask=full_schema_reask,
                disable_tracer=(not self._allow_metrics_collection),
            )
            return runner(call_log=call_log, prompt_params=prompt_params)
        else:
            # Otherwise, use Runner
            runner = Runner(
                output_type=self._output_type,
                output_schema=self.output_schema.to_dict(),
                num_reasks=num_reasks,
                validation_map=self._validator_map,
                prompt=prompt,
                instructions=instructions,
                msg_history=msg_history,
                api=api,
                metadata=metadata,
                output=llm_output,
                base_model=self._base_model,
                full_schema_reask=full_schema_reask,
                disable_tracer=(not self._allow_metrics_collection),
            )
            call = runner(call_log=call_log, prompt_params=prompt_params)
            return ValidationOutcome[OT].from_guard_history(call)

    async def _exec_async(
        self,
        *args,
        llm_api: Callable[[Any], Awaitable[Any]],
        llm_output: Optional[str] = None,
        call_log: Call,
        prompt_params: Dict = {},  # Should be defined at this point
        num_reasks: int = 0,  # Should be defined at this point
        metadata: Dict = {},  # Should be defined at this point
        full_schema_reask: bool = False,  # Should be defined at this point
        prompt: Optional[str],
        instructions: Optional[str],
        msg_history: Optional[List[Dict]],
        **kwargs,
    ) -> ValidationOutcome[OT]:
        """Call the LLM asynchronously and validate the output.

        Args:
            llm_api: The LLM API to call asynchronously (e.g. openai.Completion.acreate)
            prompt_params: The parameters to pass to the prompt.format() method.
            num_reasks: The max times to re-ask the LLM for invalid output.
            prompt: The prompt to use for the LLM.
            instructions: Instructions for chat models.
            msg_history: The message history to pass to the LLM.
            metadata: Metadata to pass to the validators.
            full_schema_reask: When reasking, whether to regenerate the full schema
                               or just the incorrect values.
                               Defaults to `True` if a base model is provided,
                               `False` otherwise.

        Returns:
            The raw text output from the LLM and the validated output.
        """
        api = (
            get_async_llm_ask(llm_api, *args, **kwargs) if llm_api is not None else None
        )
        runner = AsyncRunner(
            output_type=self._output_type,
            output_schema=self.output_schema.to_dict(),
            num_reasks=num_reasks,
            validation_map=self._validator_map,
            prompt=prompt,
            instructions=instructions,
            msg_history=msg_history,
            api=api,
            metadata=metadata,
            output=llm_output,
            base_model=self._base_model,
            full_schema_reask=full_schema_reask,
            disable_tracer=(not self._allow_metrics_collection),
        )
        # Why are we using a different method here instead of just overriding?
        call = await runner.async_run(call_log=call_log, prompt_params=prompt_params)
        return ValidationOutcome[OT].from_guard_history(call)

    @overload
    def __call__(
        self,
        llm_api: Callable,
        *args,
        prompt_params: Optional[Dict] = None,
        num_reasks: Optional[int] = None,
        prompt: Optional[str] = None,
        instructions: Optional[str] = None,
        msg_history: Optional[List[Dict]] = None,
        metadata: Optional[Dict] = None,
        full_schema_reask: Optional[bool] = None,
        stream: Optional[bool] = False,
        **kwargs,
    ) -> Union[ValidationOutcome[OT], Iterable[ValidationOutcome[OT]]]: ...

    @overload
    def __call__(
        self,
        llm_api: Callable[[Any], Awaitable[Any]],
        *args,
        prompt_params: Optional[Dict] = None,
        num_reasks: Optional[int] = None,
        prompt: Optional[str] = None,
        instructions: Optional[str] = None,
        msg_history: Optional[List[Dict]] = None,
        metadata: Optional[Dict] = None,
        full_schema_reask: Optional[bool] = None,
        **kwargs,
    ) -> Awaitable[ValidationOutcome[OT]]: ...

    def __call__(
        self,
        llm_api: Union[Callable, Callable[[Any], Awaitable[Any]]],
        *args,
        prompt_params: Optional[Dict] = None,
        num_reasks: Optional[int] = 1,
        prompt: Optional[str] = None,
        instructions: Optional[str] = None,
        msg_history: Optional[List[Dict]] = None,
        metadata: Optional[Dict] = None,
        full_schema_reask: Optional[bool] = None,
        **kwargs,
    ) -> Union[
        Union[ValidationOutcome[OT], Iterable[ValidationOutcome[OT]]],
        Awaitable[ValidationOutcome[OT]],
    ]:
        """Call the LLM and validate the output. Pass an async LLM API to
        return a coroutine.

        Args:
            llm_api: The LLM API to call
                     (e.g. openai.Completion.create or openai.Completion.acreate)
            prompt_params: The parameters to pass to the prompt.format() method.
            num_reasks: The max times to re-ask the LLM for invalid output.
            prompt: The prompt to use for the LLM.
            instructions: Instructions for chat models.
            msg_history: The message history to pass to the LLM.
            metadata: Metadata to pass to the validators.
            full_schema_reask: When reasking, whether to regenerate the full schema
                               or just the incorrect values.
                               Defaults to `True` if a base model is provided,
                               `False` otherwise.

        Returns:
            The raw text output from the LLM and the validated output.
        """
        instructions = instructions or self._exec_opts.instructions
        prompt = prompt or self._exec_opts.prompt
        msg_history = msg_history or []
        if prompt is None:
            if msg_history is not None and not len(msg_history):
                raise RuntimeError(
                    "You must provide a prompt if msg_history is empty. "
                    "Alternatively, you can provide a prompt in the Schema constructor."
                )

        return self._execute(
            *args,
            llm_api=llm_api,
            prompt_params=prompt_params,
            num_reasks=num_reasks,
            prompt=prompt,
            instructions=instructions,
            msg_history=msg_history,
            metadata=metadata,
            full_schema_reask=full_schema_reask,
            **kwargs,
        )

    @overload
    def parse(
        self,
        llm_output: str,
        *args,
        metadata: Optional[Dict] = None,
        llm_api: None = None,
        num_reasks: Optional[int] = None,
        prompt_params: Optional[Dict] = None,
        full_schema_reask: Optional[bool] = None,
        **kwargs,
    ) -> ValidationOutcome[OT]: ...

    @overload
    def parse(
        self,
        llm_output: str,
        *args,
        metadata: Optional[Dict] = None,
        llm_api: Optional[Callable[[Any], Awaitable[Any]]] = ...,
        num_reasks: Optional[int] = None,
        prompt_params: Optional[Dict] = None,
        full_schema_reask: Optional[bool] = None,
        **kwargs,
    ) -> Awaitable[ValidationOutcome[OT]]: ...

    @overload
    def parse(
        self,
        llm_output: str,
        *args,
        metadata: Optional[Dict] = None,
        llm_api: Optional[Callable] = None,
        num_reasks: Optional[int] = None,
        prompt_params: Optional[Dict] = None,
        full_schema_reask: Optional[bool] = None,
        **kwargs,
    ) -> ValidationOutcome[OT]: ...

    def parse(
        self,
        llm_output: str,
        *args,
        metadata: Optional[Dict] = None,
        llm_api: Optional[Callable] = None,
        num_reasks: Optional[int] = None,
        prompt_params: Optional[Dict] = None,
        full_schema_reask: Optional[bool] = None,
        **kwargs,
    ) -> Union[ValidationOutcome[OT], Awaitable[ValidationOutcome[OT]]]:
        """Alternate flow to using Guard where the llm_output is known.

        Args:
            llm_output: The output being parsed and validated.
            metadata: Metadata to pass to the validators.
            llm_api: The LLM API to call
                     (e.g. openai.Completion.create or openai.Completion.acreate)
            num_reasks: The max times to re-ask the LLM for invalid output.
            prompt_params: The parameters to pass to the prompt.format() method.
            full_schema_reask: When reasking, whether to regenerate the full schema
                               or just the incorrect values.

        Returns:
            The validated response. This is either a string or a dictionary,
                determined by the object schema defined in the RAILspec.
        """
        final_num_reasks = (
            num_reasks
            if num_reasks is not None
            else self._num_reasks
            if self._num_reasks is not None
            else 0
            if llm_api is None
            else 1
        )
        prompt = kwargs.pop("prompt", self._exec_opts.prompt)
        instructions = kwargs.pop("instructions", self._exec_opts.instructions)
        msg_history = kwargs.pop("msg_history")

        return self._execute(
            *args,
            llm_output=llm_output,
            llm_api=llm_api,
            prompt_params=prompt_params,
            num_reasks=final_num_reasks,
            prompt=prompt,
            instructions=instructions,
            msg_history=msg_history,
            metadata=metadata,
            full_schema_reask=full_schema_reask,
            **kwargs,
        )

    def __add_validator(self, validator: Validator, on: str = "output"):
        # TODO: This isn't the case anymore; should we remove this restriction?
        # e.g. User could rightfully do:
        # Guard.from_pydantic().use(Validator, on="$.some.prop")
        if self._output_type != OutputTypes.STRING:
            raise RuntimeError(
                "The `use` method is only available for string output types."
            )

        if on == "output":
            on = "$"

        validator_reference = ValidatorReference(
            id=validator.rail_alias,
            on=on,
            on_fail=validator.on_fail_descriptor,
            kwargs=validator.get_args(),
        )
        self.validators.append(validator_reference)
        self._validator_map[on] = self._validator_map.get(on, [])
        self._validator_map[on].append(validator)
        self._validators.append(validator)

    @overload
    def use(self, validator: Validator, *, on: str = "output") -> "Guard": ...

    @overload
    def use(
        self, validator: Type[Validator], *args, on: str = "output", **kwargs
    ) -> "Guard": ...

    def use(
        self,
        validator: UseValidatorSpec,
        *args,
        on: str = "output",
        **kwargs,
    ) -> "Guard":
        """Use a validator to validate either of the following:
        - The output of an LLM request
        - The prompt
        - The instructions
        - The message history

        *Note*: For on="output", `use` is only available for string output types.

        Args:
            validator: The validator to use. Either the class or an instance.
            on: The part of the LLM request to validate. Defaults to "output".
        """
        hydrated_validator = get_validator(validator, *args, **kwargs)
        self.__add_validator(hydrated_validator, on=on)
        return self

    @overload
    def use_many(self, *validators: Validator, on: str = "output") -> "Guard": ...

    @overload
    def use_many(
        self,
        *validators: UseManyValidatorTuple,
        on: str = "output",
    ) -> "Guard": ...

    def use_many(
        self,
        *validators: UseManyValidatorSpec,
        on: str = "output",
    ) -> "Guard":
        """Use a validator to validate results of an LLM request.

        *Note*: `use_many` is only available for string output types.
        """
        if self.rail.output_type != "str":
            raise RuntimeError(
                "The `use_many` method is only available for string output types."
            )

        # Loop through the validators
        for v in validators:
            hydrated_validator = get_validator(v)
            self.__add_validator(hydrated_validator, on=on)
        return self

    def validate(self, llm_output: str, *args, **kwargs) -> ValidationOutcome[str]:
        return self.parse(llm_output=llm_output, *args, **kwargs)

    # No call support for this until
    # https://github.com/guardrails-ai/guardrails/pull/525 is merged
    # def __call__(self, llm_output: str, *args, **kwargs) -> ValidationOutcome[str]:
    #     return self.validate(llm_output, *args, **kwargs)

    def invoke(
        self, input: InputType, config: Optional[RunnableConfig] = None
    ) -> InputType:
        output = BaseMessage(content="", type="")
        str_input = None
        input_is_chat_message = False
        if isinstance(input, BaseMessage):
            input_is_chat_message = True
            str_input = str(input.content)
            output = deepcopy(input)
        else:
            str_input = str(input)

        response = self.validate(str_input)

        validated_output = response.validated_output
        if not validated_output:
            raise ValidationError(
                (
                    "The response from the LLM failed validation!"
                    "See `guard.history` for more details."
                )
            )

        if isinstance(validated_output, Dict):
            validated_output = json.dumps(validated_output)

        if input_is_chat_message:
            output.content = validated_output
            return cast(InputType, output)
        return cast(InputType, validated_output)

    def upsert_guard(self):
        if self._api_client:
            self._api_client.upsert_guard(self)
        else:
            raise ValueError("Guard does not have an api client!")

    def _call_server(
        self,
        *args,
        llm_output: Optional[str] = None,
        llm_api: Optional[Callable] = None,
        num_reasks: Optional[int] = None,
        prompt_params: Optional[Dict] = None,
        metadata: Optional[Dict] = {},
        full_schema_reask: Optional[bool] = True,
        call_log: Optional[Call],
        # prompt: Optional[str],
        # instructions: Optional[str],
        # msg_history: Optional[List[Dict]],
        **kwargs,
    ):
        if self._api_client:
            payload: Dict[str, Any] = {"args": list(args)}
            payload.update(**kwargs)
            if llm_output is not None:
                payload["llmOutput"] = llm_output
            if num_reasks is not None:
                payload["numReasks"] = num_reasks
            if prompt_params is not None:
                payload["promptParams"] = prompt_params
            if llm_api is not None:
                payload["llmApi"] = get_llm_api_enum(llm_api)
            # TODO: get enum for llm_api
            validation_output: Optional[Any] = self._api_client.validate(
                guard=self,  # type: ignore
                payload=ValidatePayload.from_dict(payload),
                openai_api_key=get_call_kwarg("api_key"),
            )

            if not validation_output:
                return ValidationOutcome[OT](
                    raw_llm_output=None,
                    validated_output=None,
                    validation_passed=False,
                    error="The response from the server was empty!",
                )

            # TODO: GET /guard/{guard-name}/history
            call_log = call_log or Call()
            if llm_api is not None:
                llm_api = get_llm_ask(llm_api)
                if asyncio.iscoroutinefunction(llm_api):
                    llm_api = get_async_llm_ask(llm_api)
            session_history = (
                validation_output.session_history
                if validation_output is not None and validation_output.session_history
                else []
            )
            history: List[Call]
            for history in session_history:
                history_events: Optional[List[Any]] = (  # type: ignore
                    history.history
                )
                if history_events is None:
                    continue

                iterations = [
                    Iteration(
                        inputs=Inputs(
                            llm_api=llm_api,
                            llm_output=llm_output,
                            instructions=(
                                Instructions(h.instructions) if h.instructions else None
                            ),
                            prompt=(
                                Prompt(h.prompt.source)  # type: ignore
                                if h.prompt
                                else None
                            ),
                            prompt_params=prompt_params,
                            num_reasks=(num_reasks or 0),
                            metadata=metadata,
                            full_schema_reask=full_schema_reask,
                        ),
                        outputs=Outputs(
                            llm_response_info=LLMResponse(
                                output=h.output  # type: ignore
                            ),
                            raw_output=h.output,
                            parsed_output=(
                                h.parsed_output.to_dict()
                                if isinstance(h.parsed_output, Any)
                                else h.parsed_output
                            ),
                            validation_output=(
                                h.validated_output.to_dict()
                                if isinstance(h.validated_output, Any)
                                else h.validated_output
                            ),
                            reasks=list(
                                [
                                    FieldReAsk(
                                        incorrect_value=r.to_dict().get(
                                            "incorrect_value"
                                        ),
                                        path=r.to_dict().get("path"),
                                        fail_results=[
                                            FailResult(
                                                error_message=r.to_dict().get(
                                                    "error_message"
                                                ),
                                                fix_value=r.to_dict().get("fix_value"),
                                            )
                                        ],
                                    )
                                    for r in h.reasks  # type: ignore
                                ]
                                if h.reasks is not None
                                else []
                            ),
                        ),
                    )
                    for h in history_events
                ]
                call_log.iterations.extend(iterations)
                if self._history.length == 0:
                    self._history.push(call_log)

            # Our interfaces are too different for this to work right now.
            # Once we move towards shared interfaces for both the open source
            # and the api we can re-enable this.
            # return ValidationOutcome[OT].from_guard_history(call_log)
            return ValidationOutcome[OT](
                raw_llm_output=validation_output.raw_llm_response,  # type: ignore
                validated_output=cast(OT, validation_output.validated_output),
                validation_passed=validation_output.result,
            )
        else:
            raise ValueError("Guard does not have an api client!")
